"""Security and behavior tests for the Phase 6 safe tool ecosystem."""

import json
import io
import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.agent.planner import WorkflowPlanner
from backend.app.config import Settings
from backend.app.core.models import WorkflowDefinition
from backend.app.safety.exceptions import (
    ExecutionLimitExceededException,
    InvalidWorkflowSchemaError,
    PathTraversalViolation,
    SSRFSecurityViolation,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)
from backend.app.tools.file_ops import (
    FileOperations,
    ListFilesInput,
    ReadFileInput,
    WriteFileInput,
)
from backend.app.tools.http_request import (
    HttpRequestInput,
    HttpRequestTool,
    PinnedSocketTransport,
)
from backend.app.tools.json_transform import JsonTransformInput, JsonTransformer
from backend.app.tools.registry import ToolRegistry, create_default_tool_registry
from backend.app.tools.search import LocalSearchProvider, SearchInput, SearchTool


@dataclass
class FakeResponse:
    status: int = 200
    headers: dict[str, str] | None = None
    body: bytes = b"ok"

    def __post_init__(self):
        if self.headers is None:
            self.headers = {"content-type": "text/plain"}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_file_operations_read_write_and_list_with_relative_results(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = FileOperations(workspace_root=workspace)
    (workspace / "notes").mkdir()

    written = files.write(WriteFileInput(path="notes/summary.txt", content="safe"))
    assert written == {
        "path": "notes/summary.txt",
        "bytes_written": 4,
    }
    assert files.read(ReadFileInput(path="notes/summary.txt")) == {
        "path": "notes/summary.txt",
        "content": "safe",
    }
    assert files.list_files(ListFilesInput(path=".", recursive=True)) == {
        "files": [{"path": "notes/summary.txt", "size_bytes": 4}]
    }


@pytest.mark.parametrize("path", ["../outside.txt", "%252e%252e/escape", "C:\\Windows\\win.ini"])
def test_file_operations_reject_workspace_escape(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = FileOperations(workspace_root=workspace)

    with pytest.raises(PathTraversalViolation):
        files.read(ReadFileInput(path=path))


def test_file_operations_reject_symlink_escape_when_supported(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not supported by this environment.")

    with pytest.raises(PathTraversalViolation):
        FileOperations(workspace_root=workspace).read(ReadFileInput(path="link.txt"))


def test_file_operations_enforce_size_and_safe_missing_permission_errors(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = FileOperations(workspace_root=workspace, max_file_size_bytes=4)
    (workspace / "large.txt").write_text("12345", encoding="utf-8")
    with pytest.raises(ExecutionLimitExceededException):
        files.read(ReadFileInput(path="large.txt"))
    with pytest.raises(ExecutionLimitExceededException):
        files.write(WriteFileInput(path="large.txt", content="12345"))
    with pytest.raises(ToolExecutionError, match="does not exist"):
        files.read(ReadFileInput(path="missing.txt"))

    original_open = Path.open

    def denied_open(path, *args, **kwargs):
        if path.name == "private.txt":
            raise PermissionError
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    with pytest.raises(ToolExecutionError, match="Permission was denied"):
        files.read(ReadFileInput(path="private.txt"))


def test_zero_file_and_transform_limits_are_not_replaced_by_defaults(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    files = FileOperations(workspace_root=workspace, max_file_size_bytes=0)
    with pytest.raises(ExecutionLimitExceededException):
        files.write(WriteFileInput(path="limited.txt", content="x"))

    transformer = JsonTransformer(max_input_size_bytes=0)
    with pytest.raises(ExecutionLimitExceededException):
        transformer.transform(
            JsonTransformInput(operation="summarize", data="text")
        )


def test_json_transform_parse_select_and_filter():
    transformer = JsonTransformer()
    assert transformer.transform(
        JsonTransformInput(operation="parse", data='{"user":{"name":"Ada"}}')
    ) == {"result": {"user": {"name": "Ada"}}}
    assert transformer.transform(
        JsonTransformInput(
            operation="select",
            data={"items": [{"id": 1}, {"id": 2}]},
            path="$.items[1].id",
        )
    ) == {"result": 2}
    assert transformer.transform(
        JsonTransformInput(
            operation="filter",
            data=[{"name": "alpha"}, {"name": "beta"}],
            field="name",
            operator="contains",
            value="et",
        )
    ) == {"result": [{"name": "beta"}]}


def test_json_transform_supports_bounded_offline_text_operations():
    transformer = JsonTransformer()
    text = "One. Two. Three. Four."
    assert transformer.transform(
        JsonTransformInput(operation="summarize", data=text)
    ) == {"result": "One. Two. Three.", "summary": "One. Two. Three."}
    assert transformer.transform(
        JsonTransformInput(operation="extract_key_points", data=text)
    ) == {
        "result": ["One.", "Two.", "Three.", "Four."],
        "key_points": ["One.", "Two.", "Three.", "Four."],
    }


def test_json_transform_rejects_invalid_json_paths_operations_and_sizes():
    transformer = JsonTransformer(max_input_size_bytes=32, max_output_size_bytes=32)
    with pytest.raises(ToolExecutionError, match="not valid JSON"):
        transformer.transform(JsonTransformInput(operation="parse", data="{"))
    with pytest.raises(ValidationError):
        JsonTransformInput(operation="execute", data={})
    with pytest.raises(ValidationError):
        JsonTransformInput(operation="summarize", data="")
    with pytest.raises(ValidationError):
        JsonTransformInput(operation="select", data={}, path="$..secret")
    with pytest.raises(ToolExecutionError, match="does not exist"):
        transformer.transform(
            JsonTransformInput(operation="select", data={}, path="$.missing")
        )
    with pytest.raises(ExecutionLimitExceededException):
        transformer.transform(
            JsonTransformInput(operation="parse", data=json.dumps({"x": "a" * 40}))
        )
    with pytest.raises(ToolExecutionError, match="not valid JSON"):
        JsonTransformer().transform(
            JsonTransformInput(operation="parse", data='{"number": NaN}')
        )
    with pytest.raises(ExecutionLimitExceededException):
        JsonTransformer(max_input_size_bytes=100, max_output_size_bytes=10).transform(
            JsonTransformInput(
                operation="select",
                data={"result": "too large"},
                path="$.result",
            )
        )
    with pytest.raises(ValidationError):
        JsonTransformInput(
            operation="filter",
            data=[],
            field="name",
            operator="equals",
        )


def _http_tool(responses, resolver=None, **options):
    transport = FakeTransport(*responses)
    tool = HttpRequestTool(
        resolver=resolver or (lambda host, port: ["8.8.8.8"]),
        transport=transport,
        **options,
    )
    return tool, transport


def test_http_request_uses_allowed_method_and_validated_public_address():
    tool, transport = _http_tool([FakeResponse(body=b"hello")])
    result = tool.request(
        HttpRequestInput(method="GET", url="https://example.com/data")
    )

    assert result == {
        "status": 200,
        "headers": {"content-type": "text/plain"},
        "body": "hello",
    }
    assert transport.calls[0]["address"] == "8.8.8.8"
    assert transport.calls[0]["secure"] is True
    with pytest.raises(ValidationError):
        HttpRequestInput(method="TRACE", url="https://example.com/")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://service.localhost/",
        "http://example.com:0/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://224.0.0.1/",
        "http://192.0.2.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[ff02::1]/",
        "http://[2001:db8::1]/",
        "https://user:password@example.com/",
    ],
)
def test_http_request_blocks_unsafe_destinations(url):
    tool, transport = _http_tool([FakeResponse()])
    with pytest.raises(SSRFSecurityViolation):
        tool.request(HttpRequestInput(method="GET", url=url))
    assert transport.calls == []


def test_http_request_blocks_mixed_dns_answers_and_resolution_failures():
    mixed, transport = _http_tool(
        [FakeResponse()],
        resolver=lambda host, port: ["8.8.8.8", "192.168.1.1"],
    )
    with pytest.raises(SSRFSecurityViolation, match="non-public"):
        mixed.request(HttpRequestInput(method="GET", url="https://example.com"))
    assert transport.calls == []

    def failed_resolver(host, port):
        raise socket.gaierror("sensitive resolver detail")

    unresolved, _ = _http_tool([FakeResponse()], resolver=failed_resolver)
    with pytest.raises(SSRFSecurityViolation, match="could not be resolved") as error:
        unresolved.request(HttpRequestInput(method="GET", url="https://example.com"))
    assert "sensitive resolver detail" not in str(error.value)


def test_http_redirect_revalidates_destination_and_strips_credentials():
    addresses = []

    def resolver(host, port):
        addresses.append(host)
        return ["8.8.8.8"]

    tool, transport = _http_tool(
        [
            FakeResponse(status=302, headers={"location": "https://other.example/path"}),
            FakeResponse(body=b"done"),
        ],
        resolver=resolver,
    )
    result = tool.request(
        HttpRequestInput(
            method="GET",
            url="https://example.com/start",
            headers={
                "Authorization": "Bearer secret",
                "X-API-Key": "private-key",
                "Accept": "text/plain",
            },
        )
    )

    assert result["body"] == "done"
    assert addresses == ["example.com", "other.example"]
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert "Authorization" not in transport.calls[1]["headers"]
    assert "X-API-Key" not in transport.calls[1]["headers"]


def test_http_same_origin_redirect_preserves_validated_caller_headers():
    tool, transport = _http_tool(
        [
            FakeResponse(status=302, headers={"location": "https://EXAMPLE.com:443/next"}),
            FakeResponse(body=b"done"),
        ]
    )
    headers = {
        "Authorization": "Bearer same-origin-secret",
        "Accept": "application/json",
        "X-Trace-Id": "trace-123",
    }

    tool.request(
        HttpRequestInput(
            method="GET",
            url="https://example.com/start",
            headers=headers,
        )
    )

    assert transport.calls[1]["headers"] == headers


@pytest.mark.parametrize(
    "credential_header",
    [
        "Authorization",
        "Cookie",
        "X-AuthToken",
        "X-API-Key",
        "X-Private-Credential",
    ],
)
def test_http_cross_origin_redirect_strips_all_non_allowlisted_headers(
    credential_header,
):
    tool, transport = _http_tool(
        [
            FakeResponse(status=302, headers={"location": "https://other.example/next"}),
            FakeResponse(body=b"done"),
        ]
    )

    tool.request(
        HttpRequestInput(
            method="GET",
            url="https://example.com/start",
            headers={
                "Accept": "application/json",
                "User-Agent": "FlowPilot-test",
                credential_header: "must-not-leak",
            },
        )
    )

    assert transport.calls[1]["headers"] == {
        "Accept": "application/json",
        "User-Agent": "FlowPilot-test",
    }


@pytest.mark.parametrize(
    ("url", "location"),
    [
        ("http://example.com/start", "https://example.com/next"),
        ("https://example.com/start", "https://example.com:8443/next"),
        ("https://example.com/start", "https://other.example/next"),
    ],
    ids=["scheme-change", "port-change", "hostname-change"],
)
def test_http_redirect_origin_changes_strip_credentials(url, location):
    tool, transport = _http_tool(
        [
            FakeResponse(status=302, headers={"location": location}),
            FakeResponse(body=b"done"),
        ]
    )

    tool.request(
        HttpRequestInput(
            method="GET",
            url=url,
            headers={
                "Authorization": "Bearer must-not-leak",
                "Accept-Language": "en",
                "X-Custom-Credential": "must-not-leak",
            },
        )
    )

    assert transport.calls[1]["headers"] == {"Accept-Language": "en"}


def test_http_request_rejects_cross_origin_body_redirect_and_bad_headers():
    tool, transport = _http_tool(
        [FakeResponse(status=307, headers={"location": "https://other.example/next"})]
    )
    with pytest.raises(SSRFSecurityViolation, match="with bodies"):
        tool.request(
            HttpRequestInput(
                method="POST",
                url="https://example.com",
                body="private payload",
            )
        )
    assert len(transport.calls) == 1

    with pytest.raises(ToolExecutionError, match="header is invalid"):
        tool.request(
            HttpRequestInput(
                method="GET",
                url="https://example.com",
                headers={"X-Test": "bad\r\nInjected: value"},
            )
        )
    assert len(transport.calls) == 1


def test_http_redirect_to_private_ip_is_rejected():
    tool, transport = _http_tool(
        [FakeResponse(status=302, headers={"location": "http://127.0.0.1/admin"})]
    )
    with pytest.raises(SSRFSecurityViolation):
        tool.request(HttpRequestInput(method="GET", url="http://example.com"))
    assert len(transport.calls) == 1


def test_http_transport_connects_to_pinned_ip_and_keeps_tls_hostname(monkeypatch):
    class SocketStub:
        def __init__(self, family, socket_type):
            self.family = family
            self.connected_to = None
            self.sent = bytearray()

        def settimeout(self, timeout):
            pass

        def connect(self, address):
            self.connected_to = address

        def sendall(self, data):
            self.sent.extend(data)

        def makefile(self, mode):
            return io.BytesIO(
                b"HTTP/1.1 302 Found\r\n"
                b"Content-Length: 2\r\n"
                b"Content-Type: text/plain\r\n"
                b"Location: /next\r\n\r\nok"
            )

        def close(self):
            pass

    socket_stub = SocketStub(socket.AF_INET, socket.SOCK_STREAM)
    monkeypatch.setattr(
        "backend.app.tools.http_request.socket.socket",
        lambda family, socket_type: socket_stub,
    )
    tls_hosts = []

    class ContextStub:
        def wrap_socket(self, sock, server_hostname):
            tls_hosts.append(server_hostname)
            return sock

    monkeypatch.setattr(
        "backend.app.tools.http_request.ssl.create_default_context",
        lambda: ContextStub(),
    )
    response = PinnedSocketTransport().request(
        hostname="example.com",
        port=443,
        address="8.8.8.8",
        secure=True,
        method="GET",
        target="/",
        headers={},
        body=None,
        connect_timeout=1.0,
        read_timeout=1.0,
        max_response_bytes=16,
    )
    assert socket_stub.connected_to == ("8.8.8.8", 443)
    assert tls_hosts == ["example.com"]
    assert response.status == 302
    assert response.body == b"ok"
    assert response.headers["location"] == "/next"


def test_http_request_enforces_timeout_payload_response_and_redacts_errors(caplog):
    tool, _ = _http_tool([socket.timeout("Bearer top-secret")])
    with pytest.raises(ToolExecutionError, match="timed out") as error:
        tool.request(
            HttpRequestInput(
                method="POST",
                url="https://example.com",
                headers={"Authorization": "Bearer top-secret"},
            )
        )
    assert "top-secret" not in str(error.value)
    assert "top-secret" not in caplog.text

    oversized, _ = _http_tool(
        [FakeResponse(body=b"x" * 5)],
        max_response_size_bytes=4,
    )
    with pytest.raises(ExecutionLimitExceededException):
        oversized.request(HttpRequestInput(method="GET", url="https://example.com"))

    large_body, _ = _http_tool(
        [FakeResponse()],
        max_payload_size_bytes=4,
    )
    with pytest.raises(ExecutionLimitExceededException):
        large_body.request(
            HttpRequestInput(method="POST", url="https://example.com", body="12345")
        )


def test_http_redirect_limit_and_downgrade_are_rejected():
    redirect, _ = _http_tool(
        [FakeResponse(status=302, headers={"location": "/next"})],
        max_redirects=0,
    )
    with pytest.raises(ToolExecutionError, match="redirect limit"):
        redirect.request(HttpRequestInput(method="GET", url="https://example.com"))

    downgrade, _ = _http_tool(
        [FakeResponse(status=302, headers={"location": "http://example.com/next"})]
    )
    with pytest.raises(SSRFSecurityViolation, match="downgrade"):
        downgrade.request(HttpRequestInput(method="GET", url="https://example.com"))


def test_tool_registry_authorizes_registered_ids_and_validates_before_call(tmp_path):
    config = Settings(_env_file=None, workspace_root=tmp_path)
    registry = create_default_tool_registry(config)
    assert "file.read" in registry.identifiers()
    assert "shell" not in registry.identifiers()
    assert "file.read" in registry.schemas()

    with pytest.raises(ToolNotFoundError):
        registry.execute("shell", {})
    with pytest.raises(ToolValidationError):
        registry.execute("file.read", {"path": "safe.txt", "unexpected": True})
    assert registry.execute(
        "file.write",
        {"path": "safe.txt", "content": "hello"},
    )["bytes_written"] == 5


def test_tool_registry_rejects_duplicate_and_unknown_workflow_tools():
    registry = ToolRegistry()
    registry.register("search.query", SearchInput, lambda request: request.query)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("search.query", SearchInput, lambda request: request.query)
    with pytest.raises(ToolNotFoundError):
        registry.validate_workflow(
            WorkflowDefinition(
                tasks=[
                    {
                        "id": "task",
                        "name": "Unknown",
                        "tool": "not.registered",
                    }
                ]
            )
        )


def test_planner_checks_planned_tools_against_authorized_registry():
    planner = WorkflowPlanner(
        config=Settings(_env_file=None),
        tool_registry=ToolRegistry(),
    )
    workflow = WorkflowDefinition(
        tasks=[
            {
                "id": "task",
                "name": "Transform",
                "tool": "transform",
                "input_parameters": {"operation": "summarize", "text": "words"},
            }
        ]
    )
    with pytest.raises(InvalidWorkflowSchemaError, match="unauthorized"):
        planner._validate_generated_workflow(workflow)


def test_local_search_is_bounded_read_only_and_returns_structured_results(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(
        "the target phrase\nanother TARGET phrase\n", encoding="utf-8"
    )
    search = SearchTool(
        provider=LocalSearchProvider(FileOperations(workspace_root=workspace)),
        max_results=1,
        max_response_size_bytes=1024,
    )
    assert search.search(SearchInput(query="target", max_results=1)) == {
        "results": [
            {"path": "notes.txt", "line": 1, "text": "the target phrase"}
        ]
    }
    with pytest.raises(ValidationError):
        SearchInput(query="x" * 257)
    with pytest.raises(ExecutionLimitExceededException):
        search.search(SearchInput(query="target", max_results=2))

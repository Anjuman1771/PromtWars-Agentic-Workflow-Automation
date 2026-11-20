"""Restricted HTTP requests with validated DNS pinning and bounded responses."""

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable, Sequence
from typing import Any, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ..config import settings
from ..safety.exceptions import (
    ExecutionLimitExceededException,
    SSRFSecurityViolation,
    ToolExecutionError,
)

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_SAFE_CROSS_ORIGIN_REDIRECT_HEADERS = {
    "accept",
    "accept-language",
    "user-agent",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class HttpRequestInput(BaseModel):
    """Input schema for one bounded HTTP request."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(pattern=r"^(GET|HEAD|POST|PUT|PATCH|DELETE)$")
    url: StrictStr = Field(min_length=1, max_length=4096)
    headers: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    body: StrictStr | None = None


class HttpResponse(Protocol):
    """Transport response contract used by the HTTP tool and deterministic tests."""

    status: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    """Transport contract that connects only to a previously validated address."""

    def request(
        self,
        *,
        hostname: str,
        port: int,
        address: str,
        secure: bool,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None,
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class _TransportResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


class PinnedSocketTransport:
    """HTTP transport that never performs a second DNS lookup during connect."""

    def request(
        self,
        *,
        hostname: str,
        port: int,
        address: str,
        secure: bool,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None,
        connect_timeout: float,
        read_timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        ip = ipaddress.ip_address(address)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sockaddr: tuple[Any, ...] = (
            (str(ip), port, 0, 0) if ip.version == 6 else (str(ip), port)
        )
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(connect_timeout)
            sock.connect(sockaddr)
            sock.settimeout(read_timeout)
            if secure:
                sock = ssl.create_default_context().wrap_socket(
                    sock,
                    server_hostname=hostname,
                )
                sock.settimeout(read_timeout)

            connection = http.client.HTTPConnection(
                hostname,
                port,
                timeout=read_timeout,
            )
            connection.sock = sock
            sock = None
            request_headers = {
                name: value
                for name, value in headers.items()
                if name.casefold() != "accept-encoding"
            }
            request_headers["Accept-Encoding"] = "identity"
            connection.request(method, target, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read(max_response_bytes + 1)
            content_type = response.getheader("Content-Type")
            safe_headers: dict[str, str] = {}
            if content_type is not None:
                safe_headers["content-type"] = content_type
            location = response.getheader("Location")
            if location is not None:
                safe_headers["location"] = location
            return _TransportResponse(response.status, safe_headers, payload)
        finally:
            if sock is not None:
                sock.close()
            if "connection" in locals():
                connection.close()


Resolver = Callable[[str, int], Sequence[str]]


class HttpRequestTool:
    """Issue outbound requests only to globally routable, DNS-pinned addresses."""

    def __init__(
        self,
        resolver: Resolver | None = None,
        transport: HttpTransport | None = None,
        max_response_size_bytes: int | None = None,
        max_payload_size_bytes: int | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        max_redirects: int | None = None,
    ) -> None:
        self.resolver = resolver or self._resolve
        self.transport = transport or PinnedSocketTransport()
        self.max_response_size_bytes = (
            settings.max_response_size_bytes
            if max_response_size_bytes is None
            else max_response_size_bytes
        )
        self.max_payload_size_bytes = (
            settings.max_payload_size_bytes
            if max_payload_size_bytes is None
            else max_payload_size_bytes
        )
        self.connect_timeout_seconds = (
            settings.tool_http_connect_timeout_seconds
            if connect_timeout_seconds is None
            else connect_timeout_seconds
        )
        self.read_timeout_seconds = (
            settings.tool_http_read_timeout_seconds
            if read_timeout_seconds is None
            else read_timeout_seconds
        )
        self.max_redirects = (
            settings.tool_http_max_redirects if max_redirects is None else max_redirects
        )

    def request(self, request: HttpRequestInput) -> dict[str, Any]:
        """Perform the request, validating and pinning every redirect destination."""
        if request.body is not None and len(request.body) > self.max_payload_size_bytes:
            raise ExecutionLimitExceededException(
                "The HTTP request body exceeds the configured payload-size limit."
            )
        try:
            body = request.body.encode("utf-8") if request.body is not None else None
        except UnicodeEncodeError:
            raise ToolExecutionError("The HTTP request body is not valid UTF-8.") from None
        if body is not None and len(body) > self.max_payload_size_bytes:
            raise ExecutionLimitExceededException(
                "The HTTP request body exceeds the configured payload-size limit."
            )
        headers = self._validate_headers(request.headers)
        current_url = request.url
        method = request.method
        current_body = body
        redirects = 0

        while True:
            _, hostname, port, secure, target = self._parse_url(current_url)
            addresses = self._validated_addresses(hostname, port)
            try:
                response = self.transport.request(
                    hostname=hostname,
                    port=port,
                    address=addresses[0],
                    secure=secure,
                    method=method,
                    target=target,
                    headers=headers,
                    body=current_body,
                    connect_timeout=self.connect_timeout_seconds,
                    read_timeout=self.read_timeout_seconds,
                    max_response_bytes=self.max_response_size_bytes,
                )
            except (TimeoutError, socket.timeout):
                raise ToolExecutionError("The HTTP request timed out.") from None
            except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
                raise ToolExecutionError("The HTTP request could not be completed.") from None

            if len(response.body) > self.max_response_size_bytes:
                raise ExecutionLimitExceededException(
                    "The HTTP response exceeds the configured response-size limit."
                )

            location = self._header(response.headers, "location")
            if response.status not in _REDIRECT_STATUSES or location is None:
                break
            if redirects >= self.max_redirects:
                raise ToolExecutionError("The HTTP redirect limit was exceeded.")
            next_url = urljoin(current_url, location)
            _, next_host, next_port, next_secure, _ = self._parse_url(next_url)
            current_origin = (secure, hostname, port)
            next_origin = (next_secure, next_host, next_port)
            if secure and not next_secure:
                raise SSRFSecurityViolation("HTTPS redirects may not downgrade to HTTP.")
            if current_body is not None and current_origin != next_origin:
                raise SSRFSecurityViolation(
                    "Cross-origin redirects for requests with bodies are forbidden."
                )
            if response.status == 303 or (
                response.status in {301, 302} and method == "POST"
            ):
                method = "GET"
                current_body = None
            if current_origin != next_origin:
                headers = {
                    name: value
                    for name, value in headers.items()
                    if name.casefold() in _SAFE_CROSS_ORIGIN_REDIRECT_HEADERS
                }
            current_url = next_url
            redirects += 1

        try:
            text = response.body.decode("utf-8")
        except UnicodeDecodeError:
            text = response.body.decode("utf-8", errors="replace")
        result = {
            "status": response.status,
            "headers": {
                name: value
                for name, value in response.headers.items()
                if name.casefold() == "content-type"
            },
            "body": text,
        }
        try:
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ToolExecutionError("The HTTP response could not be represented safely.") from None
        if len(encoded) > self.max_response_size_bytes:
            raise ExecutionLimitExceededException(
                "The HTTP response exceeds the configured response-size limit."
            )
        return result

    @staticmethod
    def _resolve(hostname: str, port: int) -> Sequence[str]:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError:
            raise SSRFSecurityViolation("The HTTP destination could not be resolved.") from None
        return tuple(dict.fromkeys(str(record[4][0]) for record in records))

    def _validated_addresses(self, hostname: str, port: int) -> list[str]:
        if hostname.casefold().rstrip(".") == "localhost" or hostname.casefold().endswith(
            ".localhost"
        ):
            raise SSRFSecurityViolation("Localhost HTTP destinations are forbidden.")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                addresses = list(self.resolver(hostname, port))
            except SSRFSecurityViolation:
                raise
            except (OSError, ValueError):
                raise SSRFSecurityViolation(
                    "The HTTP destination could not be resolved."
                ) from None
        else:
            addresses = [str(literal)]

        if not addresses:
            raise SSRFSecurityViolation("The HTTP destination has no usable address.")
        validated: list[str] = []
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError:
                raise SSRFSecurityViolation(
                    "The HTTP destination resolved to an invalid address."
                ) from None
            if (
                not parsed_address.is_global
                or parsed_address.is_multicast
                or parsed_address.is_reserved
            ):
                raise SSRFSecurityViolation(
                    "The HTTP destination resolved to a non-public address."
                )
            validated.append(str(parsed_address))
        return validated

    @staticmethod
    def _parse_url(url: str) -> tuple[SplitResult, str, int, bool, str]:
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.casefold()
            if scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise ValueError
            hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
            parsed_port = parsed.port
            port = (
                (443 if scheme == "https" else 80)
                if parsed_port is None
                else parsed_port
            )
            if not 1 <= port <= 65535:
                raise ValueError
        except (UnicodeError, ValueError):
            raise SSRFSecurityViolation(
                "The HTTP URL is invalid or uses an unsupported scheme."
            ) from None
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        return parsed, hostname, port, scheme == "https", target

    def _validate_headers(self, headers: dict[str, str]) -> dict[str, str]:
        if len(headers) > 64:
            raise ToolExecutionError("The HTTP request contains too many headers.")
        validated: dict[str, str] = {}
        total_size = 0
        for name, value in headers.items():
            if (
                len(name) > 1024
                or len(value) > self.max_payload_size_bytes
                or not _HEADER_NAME.fullmatch(name)
                or name.casefold() in _FORBIDDEN_HEADERS
                or any(
                    ord(character) < 32 and character != "\t"
                    or ord(character) == 127
                    for character in value
                )
            ):
                raise ToolExecutionError("An HTTP request header is invalid or restricted.")
            try:
                total_size += len(name.encode("ascii")) + len(value.encode("latin-1"))
            except UnicodeEncodeError:
                raise ToolExecutionError("An HTTP request header is invalid or restricted.") from None
            if total_size > self.max_payload_size_bytes:
                raise ExecutionLimitExceededException(
                    "The HTTP request headers exceed the configured payload-size limit."
                )
            validated[name] = value
        return validated

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str | None:
        return next(
            (value for key, value in headers.items() if key.casefold() == name),
            None,
        )


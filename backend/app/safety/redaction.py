"""FlowPilot AI Secret Redaction Engine.

Provides regex-based and key-based scrubbing of secrets, tokens, credentials,
and private keys from logs, traces, and API payloads.
"""

import re
from typing import Any, Dict, List, Pattern, Union

_SENSITIVE_KEY_PATTERN = (
    r"(?:password|passwd|pwd|secret(?:[_-]?key)?|api[_-]?secret|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|auth|"
    r"credentials?|aws[_-]?secret[_-]?access[_-]?key|token)"
)

# Compiled regex patterns for secret detection
PATTERNS: List[tuple[str, Pattern[str]]] = [
    # Google API Key: starts with AIza and has 35 subsequent characters
    ("GOOGLE_API_KEY", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    
    # Standard JWT Token: header.payload.signature starting with eyJ
    ("JWT_TOKEN", re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]+")),
    
    # Bearer Token in text / headers
    ("BEARER_TOKEN", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-\._~\+\/]+=*")),
    
    # AWS Access Key ID (AKIA...)
    ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b")),
    
    # Private Key blocks
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z\s]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z\s]+ PRIVATE KEY-----")),
]

_QUOTED_SENSITIVE_DOUBLE = re.compile(
    rf"(?i)(['\"]?\b{_SENSITIVE_KEY_PATTERN}\b['\"]?\s*[:=]\s*)\"([^\"]*)\""
)
_QUOTED_SENSITIVE_SINGLE = re.compile(
    rf"(?i)(['\"]?\b{_SENSITIVE_KEY_PATTERN}\b['\"]?\s*[:=]\s*)'([^']*)'"
)
_UNQUOTED_SENSITIVE = re.compile(
    rf"(?i)(['\"]?\b{_SENSITIVE_KEY_PATTERN}\b['\"]?\s*[:=]\s*)"
    r"(?!['\"])(Bearer\s+\S+|Basic\s+\S+|Token\s+\S+|[^\s,;}\]]+)"
)

# Sensitive dictionary keys to redact unconditionally
SENSITIVE_KEY_NAMES = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "secret_key",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "authorization",
    "auth",
    "credentials",
    "private_key",
    "client_secret",
    "aws_secret_access_key",
}
_NORMALIZED_SENSITIVE_KEY_NAMES = {
    name.replace("-", "_") for name in SENSITIVE_KEY_NAMES
}

REDACTED_SUBSTITUTE = "[REDACTED]"


def redact_text(text: str) -> str:
    """Scan and redact known secret patterns from a string."""
    if not isinstance(text, str) or not text:
        return text

    sanitized = text

    # Apply recognizable standalone secret patterns first.
    for name, pattern in PATTERNS:
        if name == "BEARER_TOKEN":
            sanitized = pattern.sub(f"Bearer {REDACTED_SUBSTITUTE}", sanitized)
        else:
            sanitized = pattern.sub(REDACTED_SUBSTITUTE, sanitized)

    sanitized = _QUOTED_SENSITIVE_DOUBLE.sub(
        rf'\1"{REDACTED_SUBSTITUTE}"', sanitized
    )
    sanitized = _QUOTED_SENSITIVE_SINGLE.sub(
        rf"\1'{REDACTED_SUBSTITUTE}'", sanitized
    )
    sanitized = _UNQUOTED_SENSITIVE.sub(
        rf"\1{REDACTED_SUBSTITUTE}", sanitized
    )

    return sanitized


def redact_object(obj: Any) -> Any:
    """Recursively scrub secrets from dicts, lists, and primitives."""
    if isinstance(obj, str):
        return redact_text(obj)
    elif isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            key_str = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(k))
            normalized_key = key_str.lower().replace("-", "_")
            if normalized_key in _NORMALIZED_SENSITIVE_KEY_NAMES or any(
                normalized_key.startswith(f"{name}_")
                or normalized_key.endswith(f"_{name}")
                for name in _NORMALIZED_SENSITIVE_KEY_NAMES
            ):
                cleaned[k] = REDACTED_SUBSTITUTE
            else:
                cleaned[k] = redact_object(v)
        return cleaned
    elif isinstance(obj, list):
        return [redact_object(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(redact_object(item) for item in obj)
    elif isinstance(obj, set):
        return {redact_object(item) for item in obj}
    return obj

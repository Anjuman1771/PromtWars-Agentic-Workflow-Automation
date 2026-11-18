"""FlowPilot AI Secret Redaction Engine.

Provides regex-based and key-based scrubbing of secrets, tokens, credentials,
and private keys from logs, traces, and API payloads.
"""

import re
from typing import Any, Dict, List, Pattern, Union

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
    
    # AWS Secret Access Key (approx 40 alphanumeric characters with key context)
    ("AWS_SECRET_KEY", re.compile(r"(?i)(aws_secret_access_key|aws_secret_key)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?")),
    
    # Authorization header pattern (Basic, Bearer, or generic token, with optional quotes)
    ("AUTH_HEADER", re.compile(r"(?i)(['\"]?Authorization['\"]?\s*:\s*['\"]?)(Basic\s+[A-Za-z0-9+/=]+|Token\s+[A-Za-z0-9\-_]+)(['\"]?)")),
    
    # Generic password key-value pattern
    ("PASSWORD_KV", re.compile(r"(?i)(password|passwd|pwd|secret_key|api_secret)\s*[:=]\s*['\"]?([^'\"\s,;]{3,})['\"]?")),
    
    # Private Key blocks
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z\s]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z\s]+ PRIVATE KEY-----")),
]

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
}

REDACTED_SUBSTITUTE = "[REDACTED]"


def redact_text(text: str) -> str:
    """Scan and redact known secret patterns from a string."""
    if not isinstance(text, str) or not text:
        return text

    sanitized = text

    # Apply all regex pattern filters
    for name, pattern in PATTERNS:
        if name in ("PASSWORD_KV", "AWS_SECRET_KEY"):
            # Replace only the sensitive value group, keeping the key name for context
            sanitized = pattern.sub(rf"\1: {REDACTED_SUBSTITUTE}", sanitized)
        elif name == "AUTH_HEADER":
            sanitized = pattern.sub(rf"\1{REDACTED_SUBSTITUTE}\3", sanitized)
        elif name == "BEARER_TOKEN":
            sanitized = pattern.sub(f"Bearer {REDACTED_SUBSTITUTE}", sanitized)
        else:
            sanitized = pattern.sub(REDACTED_SUBSTITUTE, sanitized)

    return sanitized


def redact_object(obj: Any) -> Any:
    """Recursively scrub secrets from dicts, lists, and primitives."""
    if isinstance(obj, str):
        return redact_text(obj)
    elif isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k).lower().replace("-", "_")
            if any(sensitive in key_str for sensitive in SENSITIVE_KEY_NAMES):
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

"""Credential scrubbing for persisted probe diagnostics and raw call output."""

import re


def redact(text: str) -> str:
    text = re.sub(r"\b(?:sk|ghp|gho|github_pat)[-_][A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)
    return re.sub(
        r'(?i)(\b(?:authorization|api[_-]?key|access[_-]?token)\b["\s:=]+)'
        r'(?:Bearer\s+)?[^\s",}]+',
        r"\1[REDACTED]",
        text,
    )

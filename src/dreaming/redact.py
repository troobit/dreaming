"""Credential scrubbing for persisted evidence and diagnostics.

Categories inspected in agentic-coding/scripts/review_html/redact.py at
5d07d2f7f31675709f57b35579f72058e846fa67. That checkout declares no licence;
these expressions are independently written, with no upstream source copied.
Update the coverage fixtures in test_redact.py when changing categories.
"""

import json
import re

MARKER = "[REDACTED]"
PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----", re.S
    ),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s@]+@", re.I),
    re.compile(r"\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:sk|gh[pousr]|github_pat)[_-][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]+\b"),
)
ASSIGNMENT = re.compile(
    r"""(?ix)(\b[\w-]*(?:key|token|secret|password|passwd|pwd|authorization)\b["']?\s*[:=]\s*["']?)"""
    r"""(?!\[REDACTED\])(?:Bearer\s+)?[^\s"',;}\]]+"""
)


def _text(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub(MARKER, text)
    return ASSIGNMENT.sub(lambda match: match[1] + MARKER, text)


def redact(text: str) -> str:
    # Preserve JSON syntax so scrubbed role responses remain parseable.
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return _text(text)

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: MARKER
                if re.fullmatch(r"(?i)(?:api[_-]?key|access[_-]?token|password|secret)", key)
                else scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return _text(value) if isinstance(value, str) else value

    cleaned = scrub(value)
    return text if cleaned == value else json.dumps(cleaned, ensure_ascii=False)

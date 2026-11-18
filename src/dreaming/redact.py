"""Credential and token redaction for stored and displayed evidence (6.2).

Seeded from agentic-coding `scripts/review_html/redact.py` at revision
e53d1935035a8a26149b8d1d4e19a56e1e9a8844 (2026-09-05, Arjen Schwarz). That
repository declares no licence (no LICENSE file at 5d07d2f7, 2026-11-15). The
patterns are copied rather than imported, so the package has no path into that
checkout (14.4); there is no update duty beyond this record.

Changes from the source: the `sk-`, `github_pat_`, `AIza` and JSON Web Token
patterns are added, and `redact` repeats until nothing changes, so redacting a
redacted text changes nothing.
"""

import re

PATTERNS: list[re.Pattern] = [
    # From the source, in its order.
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]+"),
    re.compile(
        r"""(?i)[A-Za-z0-9_]*(key|token|secret|password|passwd|pwd)["']?\s*[=:]\s*\S+"""
    ),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    # Added here.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
]

REDACTED = "[redacted]"


def _once(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(text: str) -> str:
    """`text` with every credential or token replaced by `[redacted]`.

    Each match consumes the keyword or prefix it needs and the replacement
    contains none, so repeating reaches a text no pattern matches."""
    while True:
        redacted = _once(text)
        if redacted == text:
            return text
        text = redacted

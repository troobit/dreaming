"""The public-repository scan (9.12).

Before a change is committed to a repository configured as public, the text it
adds and its commit message are checked for credentials and tokens (the
redaction patterns of `redact.py`), names from the private names list, and
transcript text: a run of at least `scan.quote_chars` characters from the
quotes of the evidence the author was given that is not already in the
repository. Whitespace differences do not hide a copied run. A run already in
the repository is found with `git grep` per line, so one that spans a line
break there still counts as copied; the scan errs towards withholding.

Problems name what was found and where, never the matched text, so neither a
credential nor a listed name reaches the report (design, Instruction set and
snapshot).
"""

import json
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from dreaming import redact

MESSAGE = "the commit message"


def normalise(text: str) -> str:
    """Whitespace collapsed to single spaces, as `observe` compares quotes."""
    return " ".join(text.split())


def added_text(diff: str) -> dict[str, str]:
    """The lines a unified diff adds, by the path they are added to."""
    added: dict[str, list[str]] = {}
    path, in_hunk = None, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and path and line.startswith("+"):
            added.setdefault(path, []).append(line[1:])
    return {path: "\n".join(lines) for path, lines in added.items()}


def evidence_quotes(
    conn: sqlite3.Connection, evidence: Iterable[str]
) -> list[tuple[str, str]]:
    """The stored quotes of the observations among `evidence`, by evidence id."""
    rows = conn.execute(
        "SELECT evidence_id, quote FROM observations"
        " WHERE evidence_id IN (SELECT value FROM json_each(?)) ORDER BY evidence_id",
        (json.dumps(sorted(set(evidence))),),
    )
    return [(row[0], row[1]) for row in rows]


def _in_repository(repo: Path, revision: str, text: str) -> bool:
    found = subprocess.run(
        ["git", "-C", str(repo), "grep", "-q", "-F", "-e", text, revision, "--"],
        capture_output=True,
    )
    return found.returncode == 0


def _copied(text: str, quote: str, length: int, repo: Path, revision: str) -> bool:
    """Whether `text` holds a run of `length` characters of `quote` that the
    repository does not already hold."""
    quote, text = normalise(quote), normalise(text)
    for start in range(len(quote) - length + 1):
        window = quote[start : start + length]
        if window in text and not _in_repository(repo, revision, window):
            return True
    return False


def _names_pattern(names: Sequence[str]) -> re.Pattern | None:
    if not names:
        return None
    words = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{words})(?!\w)", re.IGNORECASE)


def scan(
    repo: Path,
    revision: str,
    diff: str,
    message: str,
    *,
    names: Sequence[str],
    quotes: Sequence[tuple[str, str]],
    quote_chars: int,
) -> list[str]:
    """What stops a change from being committed to a public repository: one
    problem per kind and place, or none. `revision` is the commit the change
    is made on."""
    texts = {**added_text(diff), MESSAGE: message}
    named = _names_pattern(names)
    problems = []
    for where, text in texts.items():
        if redact.redact(text) != text:
            problems.append(f"a credential or token in {where}")
        if named is not None and named.search(text):
            problems.append(f"a name from the names list in {where}")
        for evidence_id, quote in quotes:
            if _copied(text, quote, quote_chars, repo, revision):
                problems.append(f"transcript text from {evidence_id} in {where}")
    return problems

"""Sessions read through apsis (3.x): listing rows and session documents.

apsis owns every source format; this module only runs it and reads its versioned
shared-json documents (`docs/schemas/apsis-shared-json.v1.schema.json` in orbit).
Fields are additive within schema version 1, so unknown fields and block types
are kept as they are.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dreaming.backends import run_process
from dreaming.errors import FAILURE, DreamingError

SCHEMA_VERSION = 1
APSIS_TIMEOUT_SECONDS = 600


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _correlation_id(source: str, session_id: str | None, thread_id: str | None):
    """The identifier that matches one interaction across sources (2.4).

    apsis records `thread_id` for LM Studio and Codex exec sessions. A Codex
    rollout carries none, but Codex sends its session id as LM Studio's client
    thread id, so that id is its correlation identifier.
    """
    if thread_id:
        return thread_id
    if source == "codex":
        return session_id
    return None


@dataclass(frozen=True)
class ListingRow:
    source: str
    id: str
    project: str | None = None
    created_at: str | None = None
    file_modified_at: str | None = None
    model: str | None = None
    provider: str | None = None
    origin: str | None = None
    thread_id: str | None = None
    size_bytes: int | None = None
    request_count: int | None = None

    @property
    def correlation_id(self) -> str | None:
        return _correlation_id(self.source, self.id, self.thread_id)


@dataclass
class Listing:
    rows: list[ListingRow]
    warnings: list[dict]


@dataclass(frozen=True)
class Entry:
    index: int
    role: str
    content: list[dict]
    id: str | None = None
    timestamp: str | None = None
    model: str | None = None
    provider: str | None = None
    turn: int | None = None
    request_id: str | None = None
    unattributed: bool = False

    @property
    def location(self) -> str:
        """A location identifying this entry within the document (6.1, Q47)."""
        return self.id or f"#{self.index}"


@dataclass
class SessionDocument:
    source: str
    id: str | None
    thread_id: str | None = None
    project: str | None = None
    created_at: str | None = None
    model: str | None = None
    provider: str | None = None
    origin: str | None = None
    entries: list[Entry] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    @property
    def correlation_id(self) -> str | None:
        return _correlation_id(self.source, self.id, self.thread_id)


def _invalid(problem: str) -> DreamingError:
    return DreamingError(f"apsis output: {problem}", FAILURE, "apsis_failed")


def _check(doc: object, kind: str) -> dict:
    if not isinstance(doc, dict):
        raise _invalid("not a JSON object")
    version = doc.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise DreamingError(
            f"apsis shared-json schema_version {version!r} is not supported "
            f"(expected {SCHEMA_VERSION}); install a compatible apsis",
            FAILURE,
            "incompatible_version",
        )
    if doc.get("kind") != kind:
        raise _invalid(f"expected a {kind} document, got {doc.get('kind')!r}")
    return doc


def _list(doc: dict, key: str) -> list:
    value = doc.get(key) or []
    if not isinstance(value, list):
        raise _invalid(f"{key} is not a list")
    return value


def parse_listing(doc: object) -> Listing:
    doc = _check(doc, "listing")
    rows = []
    for row in _list(doc, "sessions"):
        if (
            not isinstance(row, dict)
            or not _text(row.get("source"))
            or not _text(row.get("id"))
        ):
            raise _invalid(f"listing row without a source and id: {row!r}")
        rows.append(
            ListingRow(
                source=row["source"],
                id=row["id"],
                project=_text(row.get("project")),
                created_at=_text(row.get("created_at")),
                file_modified_at=_text(row.get("file_modified_at")),
                model=_text(row.get("model")),
                provider=_text(row.get("provider")),
                origin=_text(row.get("origin")),
                thread_id=_text(row.get("thread_id")),
                size_bytes=_int(row.get("size_bytes")),
                request_count=_int(row.get("request_count")),
            )
        )
    return Listing(rows, _list(doc, "warnings"))


def parse_session(doc: object) -> SessionDocument:
    doc = _check(doc, "session")
    meta = doc.get("session")
    if not isinstance(meta, dict) or not _text(meta.get("source")):
        raise _invalid("session document without session metadata")
    entries = []
    for index, entry in enumerate(_list(doc, "entries")):
        if not isinstance(entry, dict):
            raise _invalid(f"entry {index} is not an object")
        content = entry.get("content") or []
        entries.append(
            Entry(
                index=index,
                role=_text(entry.get("role")) or "unknown",
                content=[block for block in content if isinstance(block, dict)],
                id=_text(entry.get("id")),
                timestamp=_text(entry.get("timestamp")),
                model=_text(entry.get("model")),
                provider=_text(entry.get("provider")),
                turn=_int(entry.get("turn")),
                request_id=_text(entry.get("request_id")),
                unattributed=entry.get("unattributed") is True,
            )
        )
    return SessionDocument(
        source=meta["source"],
        id=_text(meta.get("id")),
        thread_id=_text(meta.get("thread_id")),
        project=_text(meta.get("project")),
        created_at=_text(meta.get("created_at")),
        model=_text(meta.get("model")),
        provider=_text(meta.get("provider")),
        origin=_text(meta.get("origin")),
        entries=entries,
        warnings=_list(doc, "warnings"),
        requests=_list(doc, "requests"),
    )


@dataclass
class Apsis:
    """Runs `apsis` across all projects and parses its shared-json output."""

    command: str = "apsis"
    codex_exec_dirs: Sequence[str] = ()
    lmstudio_log_dir: str = ""
    timeout: float = APSIS_TIMEOUT_SECONDS

    @classmethod
    def from_config(cls, cfg: dict, home: Path) -> Apsis:
        """The configured roots plus the dream's own Codex exec streams (5.8).

        `--codex-exec-dir` replaces `APSIS_CODEX_EXEC_DIRS`, so the user's
        environment value is carried over first.
        """
        env_dirs = os.environ.get("APSIS_CODEX_EXEC_DIRS", "").split(os.pathsep)
        dream_dirs = [str(home / name) for name in ("runs", "probes")]
        dirs = [
            *filter(None, env_dirs),
            *cfg["apsis"]["codex_exec_dirs"],
            *(d for d in dream_dirs if Path(d).is_dir()),
        ]
        return cls(
            command=cfg["apsis"]["command"],
            codex_exec_dirs=list(dict.fromkeys(dirs)),
            lmstudio_log_dir=cfg["apsis"]["lmstudio_log_dir"],
        )

    def _roots(self) -> list[str]:
        args = []
        for directory in self.codex_exec_dirs:
            args += ["--codex-exec-dir", directory]
        if self.lmstudio_log_dir:
            args += ["--lmstudio-log-dir", self.lmstudio_log_dir]
        return args

    def _run(self, args: list[str]) -> object:
        argv = [self.command, *args]
        result = run_process(argv, timeout=self.timeout)
        if result.timed_out or result.returncode != 0:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            if result.timed_out:
                detail = f"timed out after {self.timeout:.0f}s"
            raise DreamingError(f"{' '.join(argv)}: {detail}", FAILURE, "apsis_failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise _invalid(f"not JSON ({exc})") from exc

    def list_sessions(self) -> Listing:
        args = ["--all-projects", "--list", "-f", "shared-json", *self._roots()]
        return parse_listing(self._run(args))

    def session(self, source: str, session_id: str) -> SessionDocument:
        args = [
            "--all-projects",
            "-s",
            source,
            "-f",
            "shared-json",
            *self._roots(),
            session_id,
        ]
        return parse_session(self._run(args))

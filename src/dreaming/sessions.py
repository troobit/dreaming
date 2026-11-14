"""Typed consumer of apsis shared-json v1; source parsing stays in apsis.

The vendored schema is from ArjenSchwarz/orbit at 686ba0917a0eabefd271a3a2035ce52d36a2f266.
It is MIT licensed; see APSIS-LICENSE.txt. Update the schema and contract fixtures together.
"""

import json
import os
import subprocess
from dataclasses import dataclass, fields
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from dreaming.redact import redact

_VALIDATOR = Draft202012Validator(
    json.loads(files("dreaming").joinpath("apsis_shared_v1.schema.json").read_text())
)


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SessionMetadata:
    source: str | None = None
    id: str | None = None
    thread_id: str | None = None
    created_at: str | None = None
    time_zone: str | None = None
    project: str | None = None
    model: str | None = None
    provider: str | None = None
    origin: str | None = None
    cost: dict | None = None
    size_bytes: int | None = None
    request_count: int | None = None
    file_modified_at: str | None = None


@dataclass(frozen=True)
class ContentBlock:
    type: str
    data: dict

    @property
    def text(self) -> str | None:
        return self.data.get("text")


@dataclass(frozen=True)
class Entry:
    role: str
    content: tuple[ContentBlock, ...]
    id: str | None = None
    timestamp: str | None = None
    model: str | None = None
    provider: str | None = None
    turn: int | None = None
    request_id: str | None = None
    supplied_context: bool = False
    unattributed: bool = False
    source_line: int | None = None
    wall_time: str | None = None


@dataclass(frozen=True)
class SessionDocument:
    metadata: SessionMetadata
    entries: tuple[Entry, ...]
    warnings: tuple[dict, ...]
    requests: tuple[dict, ...] = ()
    unattributed_summary: tuple[dict, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class Listing:
    scope: dict
    sessions: tuple[SessionMetadata, ...]
    warnings: tuple[dict, ...]
    schema_version: int = 1


def _known(cls, raw: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {key: value for key, value in raw.items() if key in names}


def parse_document(text: str) -> SessionDocument | Listing:
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise SessionError("invalid_document", "apsis output is not JSON") from exc
    if not isinstance(raw, dict):
        raise SessionError("invalid_document", "Expected an object")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise SessionError("incompatible_version", "Expected apsis schema_version 1")
    errors = list(_VALIDATOR.iter_errors(raw))
    if errors:
        # Do not include instance values: malformed documents may contain credentials.
        raise SessionError("invalid_document", "Output does not match apsis shared-json v1")
    if raw["kind"] == "listing":
        return Listing(
            raw["scope"],
            tuple(SessionMetadata(**_known(SessionMetadata, row)) for row in raw["sessions"]),
            tuple(raw["warnings"]),
        )
    entries = []
    for entry in raw["entries"]:
        values = _known(Entry, entry)
        values["content"] = tuple(ContentBlock(block["type"], block) for block in entry["content"])
        entries.append(Entry(**values))
    return SessionDocument(
        SessionMetadata(**_known(SessionMetadata, raw["session"])),
        tuple(entries),
        tuple(raw["warnings"]),
        tuple(raw.get("requests", [])),
        tuple(raw.get("unattributed_summary", [])),
    )


class ApsisClient:
    def __init__(
        self, command: str = "apsis", *, timeout: float = 60, codex_exec_dirs: tuple[Path, ...] = ()
    ):
        self.command = command
        self.timeout = timeout
        self.codex_exec_dirs = codex_exec_dirs

    def _read(self, args: list[str]) -> SessionDocument | Listing:
        directories = []
        if self.codex_exec_dirs:
            directories = list(
                dict.fromkeys(
                    [
                        *(
                            p
                            for p in os.environ.get("APSIS_CODEX_EXEC_DIRS", "").split(os.pathsep)
                            if p
                        ),
                        *(str(p) for p in self.codex_exec_dirs),
                    ]
                )
            )
        flags = [part for directory in directories for part in ("--codex-exec-dir", directory)]
        try:
            result = subprocess.run(
                [self.command, *flags, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                close_fds=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise SessionError("apsis_timeout", "apsis timed out") from exc
        except OSError as exc:
            raise SessionError("apsis_unavailable", str(exc)) from exc
        if result.returncode:
            raise SessionError("apsis_failed", redact(result.stderr[:1000]))
        return parse_document(result.stdout)

    def list(self) -> Listing:
        result = self._read(["--all-projects", "--list", "-f", "shared-json"])
        if not isinstance(result, Listing):
            raise SessionError("invalid_document", "apsis returned a session for a listing")
        return result

    def open(self, session_id: str, source: str) -> SessionDocument:
        if not session_id or not source or session_id.startswith("-") or source.startswith("-"):
            raise SessionError("invalid_session", "Invalid session identifier or source")
        # apsis uses Go's flag parser, which stops parsing at the first positional argument.
        result = self._read(["--all-projects", "-s", source, "-f", "shared-json", session_id])
        if not isinstance(result, SessionDocument):
            raise SessionError("invalid_document", "apsis returned a listing for a session")
        return result

"""Sessions read through apsis (3.x), and their selection for a dream (1.x, 2.x).

apsis owns every source format; this module only runs it and reads its versioned
shared-json documents (`docs/schemas/apsis-shared-json.v1.schema.json` in orbit).
Fields are additive within schema version 1, so unknown fields and block types
are kept as they are.

Selection groups listing rows into interactions by correlation identifier,
keeps the newest unread ones within the session budget, and splits each into
reader-sized parts.
"""

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreaming import config, knowledge, store
from dreaming.backends import estimate_tokens, run_process
from dreaming.errors import FAILURE, USAGE_ERROR, DreamingError

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
    unattributed_summary: list[dict] = field(default_factory=list)

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
        unattributed_summary=_list(doc, "unattributed_summary"),
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


# Reader entries and splitting (2.6)

TRUNCATION_MARKER = "\n[dreaming: truncated"


def _block_text(block: dict) -> str:
    """One content block as reader text; usage, events and unknown types are omitted."""
    kind = block.get("type")
    if kind in ("text", "other"):
        return block.get("text") or ""
    if kind == "thinking":
        return f"[thinking] {block.get('text') or ''}"
    if kind == "tool_use":
        args = json.dumps(block.get("input"), ensure_ascii=False, sort_keys=True)
        return f"[tool_use {block.get('name') or ''}] {args}"
    if kind == "tool_result":
        label = "tool_error" if block.get("is_error") else "tool_result"
        return f"[{label}] {block.get('content') or ''}"
    if kind == "plan":
        items = [item for item in block.get("items") or [] if isinstance(item, dict)]
        return "\n".join(
            f"[{'x' if item.get('completed') else ' '}] {item.get('text') or ''}"
            for item in items
        )
    if kind == "error":
        return f"[error] {block.get('message') or ''}"
    return ""


def entry_text(entry: Entry) -> str:
    """The entry as the reader sees it, and as quotes are verified against (6.2)."""
    return "\n".join(filter(None, map(_block_text, entry.content)))


def _source_truncated(entry: Entry) -> bool:
    return any(block.get("truncated") is True for block in entry.content)


@dataclass(frozen=True)
class ReadEntry:
    """One entry as given to the reader: its location, presented role and text."""

    location: str
    role: str
    text: str
    truncated: bool = False

    def to_dict(self) -> dict:
        data = {"id": self.location, "role": self.role, "text": self.text}
        if self.truncated:
            data["truncated"] = True
        return data


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(entry: ReadEntry) -> int:
    return len(_json(entry.to_dict()).encode())


def render_part(entries: Sequence[ReadEntry]) -> str:
    """One part of a session as it is placed in the reader's input."""
    return _json([entry.to_dict() for entry in entries])


def part_tokens(entries: Sequence[ReadEntry]) -> int:
    return estimate_tokens(render_part(entries))


def _cut(entry: ReadEntry, limit: int) -> ReadEntry:
    """The entry with the longest text prefix that fits `limit` bytes, marked."""
    total = len(entry.text)

    def kept(count: int) -> ReadEntry:
        marker = (
            f"{TRUNCATION_MARKER} to fit the reader context: "
            f"{count} of {total} characters kept]"
        )
        return replace(entry, text=entry.text[:count] + marker, truncated=True)

    if _size(kept(0)) > limit:
        raise DreamingError(
            f"the reader context leaves {limit // 4} tokens for session entries, "
            "too few for one entry; raise the reader backends' context_tokens",
            USAGE_ERROR,
            "context_too_small",
        )
    low, high = 0, total
    while low < high:
        middle = (low + high + 1) // 2
        if _size(kept(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    return kept(low)


def split_entries(
    entries: Sequence[ReadEntry], max_tokens: int
) -> list[list[ReadEntry]]:
    """Consecutive parts that each fit `max_tokens` as rendered by `render_part`.

    An entry too large for a part of its own is cut with a truncation marker.
    """
    limit = max_tokens * 4  # estimate_tokens is ceil(bytes / 4)
    parts: list[list[ReadEntry]] = []
    current: list[ReadEntry] = []
    used = 2  # the brackets of the JSON array
    for entry in entries:
        size = _size(entry)
        if 2 + size > limit:
            entry = _cut(entry, limit - 2)
            size = _size(entry)
        if current and used + 1 + size > limit:
            parts.append(current)
            current, used = [], 2
        used += size + (1 if current else 0)
        current.append(entry)
    if current:
        parts.append(current)
    return parts


# Projects and scope (2.2, 2.8)


def _worktree_parent(git_file: Path) -> Path | None:
    """The main checkout of the linked worktree whose `.git` file is `git_file`."""
    try:
        line = git_file.read_text().strip()
        if not line.startswith("gitdir:"):
            return None
        gitdir = git_file.parent / line.removeprefix("gitdir:").strip()
        common = gitdir / (gitdir / "commondir").read_text().strip()
    except OSError:
        return None  # unreadable, or a submodule, which has no commondir
    common = Path(os.path.normpath(common))
    return common.parent if common.name == ".git" else None


def project_root(path: str | None) -> str | None:
    """The repository a recorded project path belongs to (2.2).

    A subdirectory counts as its repository and a linked worktree as its main
    checkout. A path no longer on disk is matched through its nearest existing
    parent; a path outside every repository is kept as recorded.
    """
    if not path:
        return None
    for candidate in (Path(path), *Path(path).parents):
        git = candidate / ".git"
        if git.is_dir():
            return str(candidate)
        if git.is_file():
            return str(_worktree_parent(git) or candidate)
    return path


def _user_project(path: str) -> str | None:
    return project_root(str(Path(path).expanduser()))


def _within(project: str | None, base: str) -> bool:
    return project is not None and Path(project).is_relative_to(base)


@dataclass(frozen=True)
class Scope:
    """A scoped dream (2.8): one repository or one workflow."""

    project: str | None = None
    workflow: str | None = None


def scope_lenses(
    lenses: Sequence[config.Lens], scope: Scope | None, cfg: dict
) -> list[config.Lens]:
    """The lenses that can apply within a scoped dream (2.8).

    A lens fits when it leaves each dimension the scope fixes unconstrained or
    includes the scope's value; dimensions the scope leaves open are judged per
    session.
    """
    if scope is None:
        return list(lenses)
    project = _user_project(scope.project) if scope.project else None
    domain = config.assign_domain(cfg, "", project) if project else None

    def fits(lens: config.Lens) -> bool:
        if scope.workflow and lens.workflows and scope.workflow not in lens.workflows:
            return False
        if project and lens.projects:
            if project not in {_user_project(p) for p in lens.projects}:
                return False
        return not (project and lens.domains and domain not in lens.domains)

    return [lens for lens in lenses if fits(lens)]


# Selection (1.3-1.5, 2.4, 2.5, 2.7)

# Content preference within one interaction (2.4): a harness or tool session,
# then a Codex exec stream, which records no prompt, then the LM Studio log.
_CONTENT_RANK = {"codex-exec": 1, "lmstudio": 2}

# Thread ids of the dream's own direct LM Studio calls: the run id, or
# `dream-probe-<hex>` for probes (5.8).
DREAM_THREAD_PREFIX = "dream-"


def parse_time(value: str | None) -> datetime | None:
    """A recorded ISO 8601 time; a time without a zone is taken as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _latest(times: Iterable[datetime | None]) -> datetime | None:
    return max((t for t in times if t is not None), default=None)


def _row_time(row: ListingRow) -> datetime | None:
    return parse_time(row.created_at) or parse_time(row.file_modified_at)


@dataclass(frozen=True)
class SelectionRequest:
    """What a dream reads. Dates, a project filter or a reread request make an
    explicit selection, which includes processed sessions from their start (1.4).
    """

    since: datetime | None = None
    until: datetime | None = None
    project: str | None = None
    reread: bool = False
    scope: Scope | None = None

    @property
    def explicit(self) -> bool:
        return bool(self.since or self.until or self.project or self.reread)

    @classmethod
    def from_dict(cls, selection: Mapping) -> SelectionRequest:
        """The request as a run records it (`runs.selection`)."""
        scope = selection.get("scope")
        return cls(
            since=parse_time(selection.get("since")),
            until=parse_time(selection.get("until")),
            project=selection.get("project"),
            reread=bool(selection.get("reread")),
            scope=Scope(**scope) if scope else None,
        )


# Attribution (2.2, 2.3, 2.9)

HARNESS_BY_SOURCE = {"codex-exec": "codex"}


@dataclass(frozen=True)
class Attribution:
    """What a session is attributed with; None means unknown (2.2)."""

    harness: str
    model: str | None  # the first recorded; entries may record others
    provider: str | None
    locality: str  # local, cloud or unknown
    project: str | None  # the repository; a worktree counts as its main checkout
    domain: str | None  # None is the unassigned domain (2.3)
    workflow: str | None  # from a registration (14.8)
    origin: str | None
    interactive: bool
    origin_mapped: bool
    registered: bool
    subject: str  # `dreaming` for sessions of earlier dreams (1.5)
    started_at: str | None
    quote_entries: frozenset[str] = frozenset()  # entries quoting the user (2.9)


def _first(values: Iterable[str | None]) -> str | None:
    return next((value for value in values if value), None)


def attribute(
    cfg: dict,
    document: SessionDocument,
    rows: Sequence[ListingRow],
    *,
    project: str | None,
    registration: Mapping | None,
    subject: str,
) -> Attribution:
    """Attribute an interaction from its content document and listing rows.

    Prompts count as the user's only in an interactive session: not one a tool
    registered, a dream started, or an origin configuration marks non-interactive
    (2.9).
    """
    model = _first(
        [document.model, *(e.model for e in document.entries), *(r.model for r in rows)]
    )
    provider = _first(
        [
            document.provider,
            *(e.provider for e in document.entries),
            *(r.provider for r in rows),
        ]
    )
    origin = _first([document.origin, *(r.origin for r in rows)])
    interactive, mapped = config.classify_origin(cfg, document.source, origin)
    registered = registration is not None
    return Attribution(
        harness=HARNESS_BY_SOURCE.get(document.source, document.source),
        model=model,
        provider=provider,
        locality=config.classify_locality(cfg, provider, model),
        project=project,
        domain=config.assign_domain(cfg, document.source, project),
        workflow=registration["workflow"] if registered else None,
        origin=origin,
        interactive=interactive and not registered and subject == "user",
        origin_mapped=mapped,
        registered=registered,
        subject=subject,
        started_at=document.created_at or _first(r.created_at for r in rows),
        quote_entries=frozenset(
            json.loads(registration["user_quote_entries"]) if registered else ()
        ),
    )


def presented_role(entry: Entry, attribution: Attribution) -> str:
    """The role the reader sees (2.9). A user entry holding only tool results is
    the tool's. A prompt in a non-interactive session is a tool's or an unknown
    author's, unless a registration marks it as quoting the user."""
    if entry.role != "user":
        return entry.role
    if entry.content and all(b.get("type") == "tool_result" for b in entry.content):
        return "tool"
    if attribution.interactive or entry.location in attribution.quote_entries:
        return "user"
    return "tool"


@dataclass
class SelectedSession:
    """One interaction to read: its content copy and every source copy (2.4)."""

    source: str
    id: str
    document: SessionDocument
    copies: list[ListingRow]
    start: int  # entries before this index were read by an earlier dream (2.5)
    attribution: Attribution
    server_requests: list[dict] = field(default_factory=list)

    def entries(self) -> list[Entry]:
        return [e for e in self.document.entries[self.start :] if not e.unattributed]

    def entry_model(self, entry: Entry) -> str | None:
        return entry.model or self.attribution.model

    def read_entries(self) -> list[ReadEntry]:
        return [
            ReadEntry(
                e.location,
                presented_role(e, self.attribution),
                entry_text(e),
                _source_truncated(e),
            )
            for e in self.entries()
        ]


@dataclass(frozen=True)
class Skipped:
    source: str
    id: str
    reason: str


@dataclass
class Selection:
    sessions: list[SelectedSession] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)  # unparseable (2.7)
    server_warnings: list[dict] = field(default_factory=list)  # report only (2.4)
    warnings: list[dict] = field(default_factory=list)  # apsis listing warnings
    # Every session the listing and registered files hold, selected or not.
    listed: dict[tuple[str, str], ListingRow] = field(default_factory=dict)

    def notes(self) -> dict:
        """What the report lists about this selection (2.3, 2.4, 2.9)."""
        return {
            "unmapped_domains": self.unmapped_domains,
            "unmapped_origins": self.unmapped_origins,
            "warnings": self.warnings,
            "server_warnings": self.server_warnings,
        }

    @property
    def unmapped_domains(self) -> list[tuple[str, str]]:
        """Sources and projects processed under the unassigned domain (2.3)."""
        return sorted(
            {
                (s.document.source, s.attribution.project or "")
                for s in self.sessions
                if s.attribution.domain is None and s.attribution.subject == "user"
            }
        )

    @property
    def unmapped_origins(self) -> list[tuple[str, str]]:
        """Origins counted as interactive only because nothing maps them (2.9)."""
        return sorted(
            {
                (s.document.source, s.attribution.origin or "")
                for s in self.sessions
                if s.attribution.interactive and not s.attribution.origin_mapped
            }
        )


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _relative(path: str, base: Path) -> Path | None:
    for root in (base, base.resolve()):
        if Path(path).is_relative_to(root):
            return Path(path).relative_to(root)
    return None


def _unchanged(seen: Mapping, row: ListingRow) -> bool:
    """A processed session whose listing size is what it was when read (2.5)."""
    sizes = (row.size_bytes, row.request_count)
    return sizes != (None, None) and sizes == (
        seen["size_bytes"],
        seen["request_count"],
    )


def _input_sessions(input_ref: str | None) -> set[tuple[str, str]]:
    """The sessions a dream call's input held, from its `input_ref`
    (`observe.call_input_ref`)."""
    try:
        found = json.loads(input_ref or "")["sessions"]
    except ValueError, TypeError, KeyError:
        return set()
    return {tuple(pair) for pair in found if isinstance(pair, list) and len(pair) == 2}


class _Selector:
    def __init__(self, conn, apsis, cfg, request, home, run_id, now) -> None:
        self.apsis = apsis
        self.cfg = cfg
        self.request = request
        self.scope = request.scope or Scope()
        self.home = home
        self.run_id = run_id
        window = timedelta(minutes=cfg["thresholds"]["recent_window_minutes"])
        self.recent_after = now - window
        self.projects = [
            _user_project(p) for p in (request.project, self.scope.project) if p
        ]
        self.seen = {
            (row["source"], row["session_id"]): row
            for row in conn.execute("SELECT * FROM sessions_seen")
        }
        self.forgotten_threads = {
            row["thread_id"]
            for row in self.seen.values()
            if row["forgotten"] and row["thread_id"]
        }
        forgotten = {key for key, row in self.seen.items() if row["forgotten"]}
        self.call_runs: dict[str, str] = {}
        # Sessions of dream calls whose input held a forgotten session (2.11).
        self.tainted: set[str] = set()
        for row in conn.execute(
            "SELECT run, session_ref, harness_session_id, input_ref FROM calls"
        ):
            refs = [r for r in (row["session_ref"], row["harness_session_id"]) if r]
            for ref in refs:
                self.call_runs[ref] = row["run"] or ""
            if forgotten and _input_sessions(row["input_ref"]) & forgotten:
                self.tainted.update(refs)
        self.registrations: dict[tuple, Mapping] = {}
        for reg in conn.execute("SELECT * FROM registrations ORDER BY registered_at"):
            self.registrations[(reg["source"], reg["session_id"])] = reg
            if reg["correlation_id"]:
                self.registrations[(reg["correlation_id"],)] = reg
        self.files: dict[str, SessionDocument] = {}
        self.selection = Selection()

    def select(self) -> Selection:
        listing = self.apsis.list_sessions()
        self.selection.warnings = list(listing.warnings)
        rows = [*listing.rows, *self._file_rows()]
        self.selection.listed = {(row.source, row.id): row for row in rows}
        groups: dict[tuple, list[ListingRow]] = {}
        for row in rows:
            key = (row.correlation_id,) if row.correlation_id else (row.source, row.id)
            groups.setdefault(key, []).append(row)
        for group in sorted(groups.values(), key=self._newest_first):
            if len(self.selection.sessions) >= self.cfg["budget"]["sessions"]:
                break
            group.sort(key=lambda r: (_CONTENT_RANK.get(r.source, 0), r.source, r.id))
            session = self._consider(group)
            if session is not None:
                self.selection.sessions.append(session)
        return self.selection

    @staticmethod
    def _newest_first(group: list[ListingRow]) -> tuple:
        started = _latest(map(_row_time, group))
        newest = -started.timestamp() if started else 0.0
        first = min((r.source, r.id) for r in group)
        return (started is None, newest, first)

    def _consider(self, group: list[ListingRow]) -> SelectedSession | None:
        if self._forgotten(group):
            return None
        run = self._dream_run(group)
        if run is not None and run == self.run_id:
            return None
        registration = self._registration(group)
        if registration and registration["project"]:
            project = _user_project(registration["project"])
        else:
            project = project_root(_first(r.project for r in group))
        workflow = registration["workflow"] if registration else None
        started = _latest(map(_row_time, group))
        if not self._matches(started, project, workflow):
            return None
        if started and started > self.recent_after:
            return None

        content = group[0]
        start = 0
        seen = self.seen.get((content.source, content.id))
        if seen is not None and not self.request.explicit:
            if seen["state"] == "failed" or _unchanged(seen, content):
                return None
            start = seen["entries_read"]
        try:
            document = self._open(content)
        except DreamingError as exc:
            self.selection.skipped.append(
                Skipped(content.source, content.id, exc.message)
            )
            return None
        docs = [document, *self._logs(group[1:])]
        last = _latest(parse_time(e.timestamp) for doc in docs for e in doc.entries)
        if last and last > self.recent_after:
            return None

        session = SelectedSession(
            source=content.source,
            id=content.id,
            document=document,
            copies=group,
            start=start,
            attribution=attribute(
                self.cfg,
                document,
                group,
                project=project,
                registration=registration,
                subject="user" if run is None else "dreaming",
            ),
            server_requests=[
                request
                for doc in docs
                if doc.source == "lmstudio"
                for request in doc.requests
            ],
        )
        if not session.entries():
            return None
        for doc in docs:
            self._server_warnings(doc)
        return session

    def _file_rows(self) -> list[ListingRow]:
        """Sessions registered as files in the shared representation (14.8)."""
        rows = []
        for key, reg in self.registrations.items():
            if key[0] != "file" or len(key) != 2:
                continue
            path = reg["session_id"]
            try:
                doc = read_session_file(Path(path))
            except DreamingError as exc:
                self.selection.skipped.append(Skipped("file", path, exc.message))
                continue
            self.files[path] = doc
            thread_id = reg["correlation_id"] or doc.correlation_id
            rows.append(
                ListingRow(
                    source="file",
                    id=path,
                    project=doc.project,
                    created_at=doc.created_at,
                    model=doc.model,
                    provider=doc.provider,
                    origin=doc.origin,
                    thread_id=thread_id,
                )
            )
        return rows

    def _open(self, row: ListingRow) -> SessionDocument:
        if row.source == "file":
            return self.files[row.id]
        return self.apsis.session(row.source, row.id)

    def _forgotten(self, group: list[ListingRow]) -> bool:
        """A forgotten session is never selected, through any copy, nor is a dream
        call session whose input held one (2.11)."""
        for row in group:
            seen = self.seen.get((row.source, row.id))
            if seen is not None and seen["forgotten"]:
                return True
            if row.correlation_id in self.forgotten_threads:
                return True
            if {row.id, row.correlation_id} & self.tainted:
                return True
        return False

    def _dream_run(self, group: list[ListingRow]) -> str | None:
        """The dream that made this interaction (1.5): its run id, "" when the run
        is unknown, or None for a session that is not a dream's."""
        for row in group:
            for ref in (row.id, row.correlation_id):
                if ref in self.call_runs:
                    return self.call_runs[ref]
            if row.thread_id and row.thread_id.startswith(DREAM_THREAD_PREFIX):
                return row.thread_id
            under_home = _relative(row.project, self.home) if row.project else None
            if under_home is not None:
                parts = under_home.parts
                return parts[1] if len(parts) > 1 and parts[0] == "runs" else ""
        return None

    def _registration(self, group: list[ListingRow]) -> Mapping | None:
        for row in group:
            for key in ((row.source, row.id), (row.correlation_id,)):
                if key in self.registrations:
                    return self.registrations[key]
        return None

    def _matches(
        self, started: datetime | None, project: str | None, workflow: str | None
    ) -> bool:
        request = self.request
        if request.since and (started is None or started < _aware(request.since)):
            return False
        if request.until and (started is None or started >= _aware(request.until)):
            return False
        if not all(_within(project, wanted) for wanted in self.projects):
            return False
        return not self.scope.workflow or workflow == self.scope.workflow

    def _logs(self, rows: list[ListingRow]) -> list[SessionDocument]:
        """LM Studio copies, which supply server metadata (2.4)."""
        logs = []
        for row in rows:
            if row.source != "lmstudio":
                continue
            try:
                logs.append(self.apsis.session(row.source, row.id))
            except DreamingError as exc:
                self.selection.warnings.append(
                    {
                        "code": "log_unavailable",
                        "message": exc.message,
                        "source": row.source,
                    }
                )
        return logs

    def _server_warnings(self, doc: SessionDocument) -> None:
        """Unattributed server responses, warnings and errors (2.4)."""
        found = [{"source": doc.source, **s} for s in doc.unattributed_summary]
        found += [
            {"source": doc.source, "role": e.role, "text": entry_text(e)}
            for e in doc.entries
            if e.unattributed
        ]
        for warning in found:
            if warning not in self.selection.server_warnings:
                self.selection.server_warnings.append(warning)


def select_sessions(
    conn: sqlite3.Connection,
    apsis: Apsis,
    cfg: dict,
    request: SelectionRequest,
    *,
    home: Path,
    run_id: str | None = None,
    now: datetime | None = None,
) -> Selection:
    """The sessions a dream reads, newest first, up to the session budget.

    Reads the store and apsis only; the Read step records what was read, so a
    dry run changes nothing (1.7).
    """
    now = now or datetime.now(UTC)
    return _Selector(conn, apsis, cfg, request, home, run_id, now).select()


# Recording what was read (2.5, 2.7, 6.6)


def _upsert_seen(conn: sqlite3.Connection, values: Mapping[str, object]) -> None:
    """Insert or update a `sessions_seen` row, touching only the given columns."""
    columns = ", ".join(values)
    placeholders = ", ".join(f":{column}" for column in values)
    updates = ", ".join(f"{column} = excluded.{column}" for column in values)
    conn.execute(
        f"INSERT INTO sessions_seen ({columns}) VALUES ({placeholders})"
        f" ON CONFLICT (source, session_id) DO UPDATE SET {updates}",
        values,
    )


def record_session(
    conn: sqlite3.Connection,
    run_id: str,
    session: SelectedSession,
    *,
    state: str = "read",
    reason: str | None = None,
    lens_versions: Mapping[str, int] | None = None,
    inventory: Mapping | None = None,
) -> None:
    """Record an interaction on every source copy, in the caller's transaction.

    Every copy takes the interaction's attribution and its listing size; the
    content copy also keeps the lens versions and how many entries were read,
    so a later dream reads only what was added (2.5), and what the part read
    used (`inventory.session_use`, 8.x). The forgotten flag is never changed
    here (2.11).
    """
    a = session.attribution
    for row in session.copies:
        values = {
            "source": row.source,
            "session_id": row.id,
            "thread_id": row.correlation_id,
            "harness": a.harness,
            "model": a.model,
            "provider": a.provider,
            "locality": a.locality,
            "project": a.project,
            "domain": a.domain,
            "workflow": a.workflow,
            "origin": a.origin,
            "interactive": int(a.interactive),
            "subject": a.subject,
            "started_at": a.started_at,
            "state": state,
            "reason": reason,
            "last_run": run_id,
            "size_bytes": row.size_bytes,
            "request_count": row.request_count,
        }
        if (row.source, row.id) == (session.source, session.id):
            values["lens_versions"] = json.dumps(dict(lens_versions or {}))
            values["entries_read"] = len(session.document.entries)
            if inventory is not None:
                values["inventory"] = json.dumps(inventory)
        _upsert_seen(conn, values)


def record_skipped(
    conn: sqlite3.Connection, run_id: str, skipped: Skipped, *, state: str = "skipped"
) -> None:
    """Record an unparseable session (2.7), or one whose reading failed (4.7),
    with its reason, keeping what an earlier dream read of it."""
    _upsert_seen(
        conn,
        {
            "source": skipped.source,
            "session_id": skipped.id,
            "state": state,
            "reason": skipped.reason,
            "last_run": run_id,
        },
    )


def _shrank(seen: Mapping, row: ListingRow) -> bool:
    """A listed session smaller than when it was read: sessions only grow, so
    its source was rewritten."""
    return any(
        now is not None and before is not None and now < before
        for now, before in (
            (row.size_bytes, seen["size_bytes"]),
            (row.request_count, seen["request_count"]),
        )
    )


def mark_availability(
    conn: sqlite3.Connection, listed: Mapping[tuple[str, str], ListingRow]
) -> None:
    """Mark the evidence of each session by what became of its source (13.7):
    `unavailable` when the listing no longer holds it, `changed` when it is
    smaller than when read, and `available` again when an unavailable one is
    listed once more. Evidence and counts are kept either way; the marker only
    tells consumers the quote can no longer be checked against its source."""
    for seen in conn.execute(
        "SELECT source, session_id, size_bytes, request_count FROM sessions_seen s"
        " WHERE EXISTS (SELECT 1 FROM observations o"
        " WHERE o.source = s.source AND o.session_id = s.session_id)"
    ).fetchall():
        row = listed.get((seen["source"], seen["session_id"]))
        if row is None:
            marks = {"available": "unavailable", "changed": "unavailable"}
        elif _shrank(seen, row):
            marks = {"available": "changed", "unavailable": "changed"}
        else:
            marks = {"unavailable": "available"}
        for before, after in marks.items():
            conn.execute(
                "UPDATE observations SET availability = ?"
                " WHERE source = ? AND session_id = ? AND availability = ?",
                (after, seen["source"], seen["session_id"], before),
            )


# Registration (2.9, 2.10, 14.8)

FILE_SOURCE = "file"
_REGISTERED = ("correlation_id", "workflow", "project", "user_quote_entries")


def _invalid_request(message: str) -> DreamingError:
    return DreamingError(message, USAGE_ERROR, "invalid_request")


def read_session_file(path: Path) -> SessionDocument:
    """A session registered as a file in the shared representation (3.4, 14.8)."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise DreamingError(
            f"cannot read session file {path}: {exc.strerror}", USAGE_ERROR, "not_found"
        ) from exc
    try:
        return parse_session(json.loads(text))
    except json.JSONDecodeError as exc:
        raise _invalid_request(f"session file {path} is not JSON ({exc})") from exc
    except DreamingError as exc:
        raise _invalid_request(f"session file {path}: {exc.message}") from exc


def _registration(details: object) -> tuple[dict, str | None]:
    """The stored row for one requested session, and the correlation identifier
    its other source copies carry (2.4)."""
    if not isinstance(details, Mapping):
        raise _invalid_request("each registered session must be an object")
    source, session_id, file = (
        _text(details.get(key)) for key in ("source", "session_id", "file")
    )
    if file and (source or session_id):
        raise _invalid_request(
            "a registered session is a file or a source and session_id, not both"
        )
    if not file and not (source and session_id):
        raise _invalid_request(
            "a registered session needs a source and session_id, or a file"
        )
    quotes = details.get("user_quote_entries") or []
    if not isinstance(quotes, list) or not all(map(_text, quotes)):
        raise _invalid_request("user_quote_entries must be a list of entry ids")
    correlation_id = _text(details.get("correlation_id"))
    if file:
        path = Path(file).expanduser().resolve()
        derived = read_session_file(path).correlation_id
        source, session_id = FILE_SOURCE, str(path)
    else:
        derived = _correlation_id(source, session_id, None)
    row = {
        "source": source,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "workflow": _text(details.get("workflow")),
        "project": _text(details.get("project")),
        "user_quote_entries": json.dumps(sorted(set(quotes))),
    }
    return row, correlation_id or derived


def _reclassify(conn: sqlite3.Connection, copies: Sequence, row: Mapping) -> list[str]:
    """Apply a registration to a session already read (2.10): its prompts become
    the tool's, except entries quoting the user, and the knowledge it supported
    is queued for reassessment."""
    sessions = [(copy["source"], copy["session_id"]) for copy in copies]
    conn.executemany(
        "UPDATE observations SET kind = ?"
        " WHERE source = ? AND session_id = ? AND kind = ?"
        " AND entry_id NOT IN (SELECT value FROM json_each(?))",
        [
            (
                knowledge.TOOL_PROMPT,
                *session,
                knowledge.USER_STATEMENT,
                row["user_quote_entries"],
            )
            for session in sessions
        ],
    )
    conn.executemany(
        "UPDATE sessions_seen SET interactive = 0, workflow = COALESCE(?, workflow)"
        " WHERE source = ? AND session_id = ?",
        [(row["workflow"], *session) for session in sessions],
    )
    items = knowledge.affected_items(conn, sessions)
    knowledge.mark_pending(conn, items)
    return items


def register_sessions(
    conn: sqlite3.Connection,
    tool: str,
    run_ref: str,
    sessions: Sequence[object],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Register sessions a tool ran, keyed by (tool, run reference, session) (14.8).

    A registered session is eligible for selection and takes the registration's
    workflow and project; its prompts count as the tool's, except entries marked
    as quoting the user (2.9). A forgotten session is refused (2.11), and
    registering again with the same details changes nothing. Every request is
    validated before anything is written.
    """
    if not (_text(tool) and _text(run_ref)):
        raise _invalid_request("a registration needs a tool name and a run reference")
    requests = [_registration(details) for details in sessions]
    registered_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    results = []
    with store.transaction(conn):
        for row, correlation_id in requests:
            key = (tool, run_ref, row["source"], row["session_id"])
            copies = knowledge.session_copies(
                conn, row["session_id"], row["source"], correlation_id
            )
            existing = conn.execute(
                "SELECT * FROM registrations"
                " WHERE tool = ? AND run_ref = ? AND source = ? AND session_id = ?",
                key,
            ).fetchone()
            if any(copy["forgotten"] for copy in copies):
                state, items = "forgotten", []
            elif existing and all(existing[k] == row[k] for k in _REGISTERED):
                state, items = "unchanged", []
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO registrations (tool, run_ref, source,"
                    " session_id, correlation_id, workflow, project,"
                    " user_quote_entries, registered_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, *(row[k] for k in _REGISTERED), registered_at),
                )
                state = "updated" if existing else "registered"
                items = _reclassify(conn, copies, row) if copies else []
            results.append(
                {
                    "source": row["source"],
                    "session_id": row["session_id"],
                    "state": state,
                    "reassess": items,
                }
            )
    return results

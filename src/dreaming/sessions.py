"""Typed consumer of apsis shared-json v1; source parsing stays in apsis.

The vendored schema is from ArjenSchwarz/orbit at 686ba0917a0eabefd271a3a2035ce52d36a2f266.
It is MIT licensed; see APSIS-LICENSE.txt. Update the schema and contract fixtures together.
"""

import json
import os
import subprocess
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from dreaming.config import Config, ConfigError, Lens, git
from dreaming.redact import redact
from dreaming.store import Store

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
    source_index: int | None = None  # Reader location; assigned from shared-json entry order.


@dataclass(frozen=True)
class SessionDocument:
    metadata: SessionMetadata
    entries: tuple[Entry, ...]
    warnings: tuple[dict, ...]
    requests: tuple[dict, ...] = ()
    unattributed_summary: tuple[dict, ...] = ()
    schema_version: int = 1
    server_metadata: tuple[SessionMetadata, ...] = ()


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
        values.pop("source_index", None)
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


@dataclass(frozen=True)
class SessionScope:
    project: str | None = None
    workflow: str | None = None
    domain: str | None = None


@dataclass(frozen=True)
class SessionAttribution:
    harness: str | None
    model: str | None
    provider: str | None
    locality: str
    project: str | None
    domain: str | None
    workflow: str | None
    started_at: str | None
    interactive: bool
    unmapped_domain: bool
    unmapped_origin: bool
    user_quote_entries: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SelectedSession:
    document: SessionDocument
    copies: tuple[SessionMetadata, ...]
    start_entry: int
    lenses: tuple[Lens, ...]
    subject: str | None = None
    copy_entry_counts: tuple[int, ...] = ()
    attribution: SessionAttribution | None = None


@dataclass(frozen=True)
class SkippedSession:
    metadata: SessionMetadata
    reason: str


@dataclass(frozen=True)
class Selection:
    sessions: tuple[SelectedSession, ...]
    skipped: tuple[SkippedSession, ...]
    diagnostics: tuple[dict, ...]


def canonical_project(project: str | None) -> str | None:
    """Collapse existing Git worktrees; leave unavailable or non-path names intact."""
    if not project:
        return None
    path = Path(project).expanduser()
    if not path.is_dir():
        return project
    try:
        common = Path(git(path, "rev-parse", "--git-common-dir"))
        common = (path / common).resolve()
        if common.name == ".git":
            return str(common.parent)
        return git(path, "rev-parse", "--show-toplevel")
    except ConfigError:
        return str(path.resolve())


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # A date-only filter is midnight UTC; source datetimes need an explicit zone.
        if len(value) == 10:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def _seen(store: Store) -> dict[tuple[str, str], dict]:
    cursor = store.connection.execute("SELECT * FROM sessions_seen")
    names = [col[0] for col in cursor.description]
    return {(row[0], row[1]): dict(zip(names, row, strict=True)) for row in cursor}


def _under(project: str | None, directory: Path) -> bool:
    return bool(
        project and Path(project).expanduser().resolve().is_relative_to(directory.resolve())
    )


def _merge_metadata(row: SessionMetadata, doc: SessionMetadata) -> SessionMetadata:
    return SessionMetadata(
        **{
            field.name: getattr(doc, field.name)
            if getattr(doc, field.name) is not None
            else getattr(row, field.name)
            for field in fields(SessionMetadata)
        }
    )


def select_sessions(
    client: ApsisClient,
    store: Store,
    config: Config,
    *,
    run_id: str,
    lenses: tuple[Lens, ...] = (),
    scope: SessionScope = SessionScope(),
    since: str | None = None,
    until: str | None = None,
    reread: bool = False,
    now: datetime | None = None,
    max_sessions: int | None = None,
) -> Selection:
    """Plan without writes or model calls. Commit cursors only after successful reading."""
    now = now or datetime.now(UTC)
    lower, upper = _timestamp(since), _timestamp(until)
    if (
        (since and lower is None)
        or (until and upper is None)
        or (lower and upper and lower >= upper)
    ):
        raise SessionError("invalid_scope", "Invalid date range")
    limit = config.budget.max_sessions if max_sessions is None else max_sessions
    if type(limit) is not int or limit < 0:
        raise SessionError("invalid_scope", "max_sessions must be a nonnegative integer")
    listing = client.list()
    from dreaming.knowledge import blocked_call_refs

    blocked_refs = blocked_call_refs(store)
    seen = _seen(store)
    rows = {(row.source, row.id): row for row in listing.sessions}
    # Registration can hand over sessions outside apsis's usual discovery directories.
    for source, sid in store.connection.execute("SELECT source,session_id FROM registrations"):
        rows.setdefault((source, sid), SessionMetadata(source=source, id=sid))
    calls = list(store.connection.execute("SELECT run_id,session_ref FROM calls"))
    current_refs = {ref for run, ref in calls if run == run_id and ref}
    previous_refs = {ref for run, ref in calls if run != run_id and ref}
    previous_refs.update(
        row[0]
        for row in store.connection.execute(
            "SELECT id FROM runs WHERE id<>?",
            (run_id,),
        )
    )
    home = store.path.parent
    skipped, diagnostics, documents = [], list(listing.warnings), []
    for key, row in rows.items():
        if seen.get(key, {}).get("forgotten"):
            skipped.append(SkippedSession(row, "forgotten"))
            continue
        try:
            path = seen.get(key, {}).get("document_path")
            if path:
                doc = parse_document(Path(path).read_text())
                if not isinstance(doc, SessionDocument):
                    raise SessionError("invalid_document", "Registered file is not a session")
            else:
                doc = client.open(row.id, row.source)
            if doc.metadata.source not in (None, row.source) or doc.metadata.id not in (
                None,
                row.id,
            ):
                raise SessionError("invalid_document", "Session identity changed")
            metadata = _merge_metadata(row, doc.metadata)
            metadata = replace(
                metadata, thread_id=metadata.thread_id or seen.get(key, {}).get("thread_id")
            )
            documents.append(replace(doc, metadata=metadata))
        except (SessionError, OSError) as exc:
            skipped.append(SkippedSession(row, redact(str(exc))))
    groups = {}
    for doc in documents:
        meta = doc.metadata
        key = ("thread", meta.thread_id) if meta.thread_id else (meta.source, meta.id)
        groups.setdefault(key, []).append(doc)
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            max(
                (
                    _timestamp(d.metadata.file_modified_at)
                    or _timestamp(d.metadata.created_at)
                    or datetime.min.replace(tzinfo=UTC)
                )
                for d in group
            ),
            min((d.metadata.source, d.metadata.id) for d in group),
        ),
        reverse=True,
    )
    selected = []
    for group in ordered:
        # Prefer a full harness transcript over a promptless exec stream or server log.
        group.sort(
            key=lambda d: (
                d.metadata.source == "lmstudio",
                d.metadata.source == "codex-exec",
                -len(d.entries),
                d.metadata.source,
                d.metadata.id,
            )
        )
        doc = group[0]
        copies = tuple(d.metadata for d in group)
        logs = tuple(m for m in copies if m.source == "lmstudio")
        doc = replace(
            doc,
            server_metadata=logs,
            metadata=replace(
                doc.metadata,
                model=doc.metadata.model or next((m.model for m in logs if m.model), None),
                provider=doc.metadata.provider
                or next((m.provider for m in logs if m.provider), None),
            ),
        )
        states = [
            row
            for row in seen.values()
            if any(
                (row["source"], row["session_id"]) == (m.source, m.id)
                or (m.thread_id and row["thread_id"] == m.thread_id)
                for m in copies
            )
        ]
        reason = None
        if any(s["forgotten"] for s in states):
            reason = "forgotten"
        elif any(m.id in blocked_refs or m.thread_id in blocked_refs for m in copies):
            reason = "forgotten_input"
        elif any(
            m.thread_id == run_id
            or m.id in current_refs
            or m.thread_id in current_refs
            or _under(m.project, home / "runs" / run_id)
            or (_under(m.project, home) and not _under(m.project, home / "runs"))
            for m in copies
        ):
            reason = "current_dream"
        elif any(
            (_timestamp(m.file_modified_at) or datetime.min.replace(tzinfo=UTC))
            > now - timedelta(seconds=config.thresholds.recent_seconds)
            for m in copies
        ):
            reason = "recent"
        if reason:
            skipped.extend(SkippedSession(m, reason) for m in copies)
            continue
        subject = (
            "dreaming"
            if any(
                m.id in previous_refs or m.thread_id in previous_refs or _under(m.project, home)
                for m in copies
            )
            else None
        )
        attribution = attribute_session(doc, store, config, copies=copies, subject=subject)
        project, domain, workflow = attribution.project, attribution.domain, attribution.workflow
        requested_project = config.repositories.get(scope.project, {}).get("path", scope.project)
        if (
            (scope.project and project != canonical_project(requested_project))
            or (scope.workflow and workflow != scope.workflow)
            or (scope.domain and domain != scope.domain)
        ):
            continue
        started = _timestamp(doc.metadata.created_at)
        if (lower and (started is None or started < lower)) or (
            upper and (started is None or started >= upper)
        ):
            continue
        applicable = tuple(
            lens
            for lens in lenses
            if lens.applies(domain=domain, project=project, workflow=workflow)
        )
        if lenses and not applicable:
            continue
        versions = {lens.id: lens.version for lens in applicable}
        start = 0
        if not (reread or since or until or scope.project):
            matching = [
                s
                for s in states
                if s["state"] == "read" and json.loads(s["lens_versions"]) == versions
            ]
            # Cursors refer to one representation, never use a log offset in a harness.
            own = [
                s
                for s in matching
                if s["source"] == doc.metadata.source and s["session_id"] == doc.metadata.id
            ]
            if own:
                start = max(s["entries_read"] for s in own)
            elif matching:
                continue  # This interaction was already read through another source.
            if any(s["state"] == "failed" for s in states):
                continue
        requests = tuple(request for d in group for request in d.requests)
        entries = []
        for entry in doc.entries:
            if entry.unattributed:
                diagnostics.append(asdict(entry))
            if doc.metadata.source == "lmstudio":
                entry = replace(
                    entry,
                    content=tuple(
                        ContentBlock(block.type, {**block.data, "truncated": True})
                        for block in entry.content
                    ),
                )
            entries.append(entry)
        for d in group:
            diagnostics.extend(d.warnings)
            diagnostics.extend(d.unattributed_summary)
            if d is not group[0]:
                diagnostics.extend(asdict(e) for e in d.entries if e.unattributed)
        doc = replace(doc, entries=tuple(entries), requests=requests, unattributed_summary=())
        if not any(not entry.unattributed for entry in doc.entries[start:]):
            continue
        if len(selected) < limit:
            selected.append(
                SelectedSession(
                    doc,
                    copies,
                    start,
                    applicable,
                    subject,
                    tuple(len(copy.entries) for copy in group),
                    attribution,
                )
            )
            if attribution.unmapped_domain:
                diagnostics.append(
                    {
                        "code": "unmapped_domain",
                        "source": doc.metadata.source,
                        "project": attribution.project,
                    }
                )
            if attribution.unmapped_origin:
                diagnostics.append(
                    {
                        "code": "unmapped_origin",
                        "source": doc.metadata.source,
                        "origin": doc.metadata.origin,
                    }
                )
    return Selection(tuple(selected), tuple(skipped), tuple(diagnostics))


def _remember(conn, metadata: SessionMetadata) -> None:
    if not metadata.source or not metadata.id:
        raise SessionError("invalid_session", "Session requires source and id")
    conn.execute(
        "INSERT INTO sessions_seen(source,session_id,thread_id,harness,model,provider,project,"
        "started_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source,session_id) DO UPDATE SET "
        "thread_id=COALESCE(excluded.thread_id,sessions_seen.thread_id)",
        (
            metadata.source,
            metadata.id,
            metadata.thread_id,
            metadata.source,
            metadata.model,
            metadata.provider,
            metadata.project,
            metadata.created_at,
        ),
    )


def record_selection(store: Store, selection: Selection) -> None:
    """Persist reportable skips separately from read-only planning."""
    with store.transaction() as conn:
        for skip in selection.skipped:
            if skip.reason in {"recent", "current_dream", "forgotten", "forgotten_input"}:
                continue
            _remember(conn, skip.metadata)
            conn.execute(
                "UPDATE sessions_seen SET state='skipped',reason=? WHERE source=? AND session_id=?",
                (redact(skip.reason), skip.metadata.source, skip.metadata.id),
            )


def mark_read(store: Store, session: SelectedSession, config: Config) -> None:
    attr = session.attribution or attribute_session(session.document, store, config)
    with store.transaction() as conn:
        if any(s["forgotten"] for s in _copy_states(store, session.copies)):
            raise SessionError("forgotten", "Session was forgotten before reading completed")
        attr = _apply_registrations(attr, _registrations(store, session.copies), config)
        for index, meta in enumerate(session.copies):
            _remember(conn, meta)
            if attr:
                conn.execute(
                    "UPDATE sessions_seen SET harness=?,model=?,provider=?,local=?,project=?,"
                    "domain=?,workflow=?,interactive=?,started_at=? "
                    "WHERE source=? AND session_id=?",
                    (
                        attr.harness,
                        attr.model,
                        attr.provider,
                        {"local": 1, "cloud": 0}.get(attr.locality),
                        attr.project,
                        attr.domain,
                        attr.workflow,
                        int(attr.interactive),
                        attr.started_at,
                        meta.source,
                        meta.id,
                    ),
                )
            conn.execute(
                "UPDATE sessions_seen SET lens_versions=?,entries_read=?,state='read',"
                "reason=NULL WHERE source=? AND session_id=? AND forgotten=0",
                (
                    json.dumps({lens.id: lens.version for lens in session.lenses}, sort_keys=True),
                    session.copy_entry_counts[index]
                    if session.copy_entry_counts
                    else len(session.document.entries),
                    meta.source,
                    meta.id,
                ),
            )


@dataclass(frozen=True)
class SessionPart:
    start_entry: int
    end_entry: int
    payload: str
    estimated_tokens: int
    estimator: str = "utf8_bytes/4"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def split_session(
    document: SessionDocument,
    *,
    context_tokens: int,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    start_entry: int = 0,
    subject: str | None = None,
    attribution: SessionAttribution | None = None,
) -> tuple[SessionPart, ...]:
    """Consecutive ranges, with entry locations preserved even when content is cut."""
    available = context_tokens - prompt_tokens - output_tokens
    if available <= 0 or min(prompt_tokens, output_tokens, start_entry) < 0:
        raise SessionError("context_too_small", "No room for session input")

    context = {}
    if subject:
        context["subject"] = subject
    if attribution:
        context["attribution"] = {
            key: value for key, value in asdict(attribution).items() if key != "user_quote_entries"
        }

    def payload(entries, start, end):
        return _json(
            {
                "schema_version": document.schema_version,
                "source": document.metadata.source,
                "session_id": document.metadata.id,
                "start_entry": start,
                "end_entry": end,
                "entries": entries,
                **context,
            }
        )

    def fits(value):
        return (len(value.encode()) + 3) // 4 <= available

    def part(entries, start, end):
        value = payload(entries, start, end)
        return SessionPart(start, end, value, (len(value.encode()) + 3) // 4)

    result, pending, start, end = [], [], start_entry, start_entry
    for position, entry in enumerate(document.entries):
        index = entry.source_index if entry.source_index is not None else position
        if index < start_entry or entry.unattributed:
            continue
        if not pending:
            start = index
        raw = {key: value for key, value in asdict(entry).items() if value is not None}
        raw["content"] = [block.data for block in entry.content]
        raw["location"] = f"entry:{index}"
        if pending and not fits(payload([*pending, raw], start, index + 1)):
            result.append(part(pending, start, index))
            pending, start = [], index
        if not fits(payload([raw], index, index + 1)):
            text = _json(raw["content"])

            def cut(size, role=entry.role, location=f"entry:{index}", content=text):
                return {
                    "role": role,
                    "location": location,
                    "content": [
                        {"type": "text", "text": content[:size] + "[TRUNCATED]", "truncated": True}
                    ],
                }

            if not fits(payload([cut(0)], index, index + 1)):
                raise SessionError("context_too_small", "Session envelope does not fit")
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if fits(payload([cut(middle)], index, index + 1)):
                    low = middle
                else:
                    high = middle - 1
            raw = cut(low)
        pending.append(raw)
        end = index + 1
    if pending:
        result.append(part(pending, start, end))
    return tuple(result)


def _copy_states(store: Store, copies: tuple[SessionMetadata, ...]) -> list[dict]:
    keys = {(m.source, m.id) for m in copies}
    threads = {m.thread_id for m in copies if m.thread_id}
    seen = _seen(store)
    threads.update(s["thread_id"] for key, s in seen.items() if key in keys and s["thread_id"])
    return [s for key, s in seen.items() if key in keys or s["thread_id"] in threads]


def _registrations(store: Store, copies: tuple[SessionMetadata, ...]) -> list[dict]:
    keys = {(m.source, m.id) for m in copies}
    keys.update((s["source"], s["session_id"]) for s in _copy_states(store, copies))
    cursor = store.connection.execute("SELECT * FROM registrations ORDER BY tool,run_ref")
    names = [col[0] for col in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor if (row[2], row[3]) in keys]


def attribute_session(
    document: SessionDocument,
    store: Store,
    config: Config,
    *,
    copies: tuple[SessionMetadata, ...] = (),
    subject: str | None = None,
) -> SessionAttribution:
    meta = document.metadata
    registrations = _registrations(store, copies or (meta,))
    project = canonical_project(meta.project)
    interactive, unmapped = config.origin(meta.source, meta.origin)
    if subject == "dreaming":
        interactive = False
    model = meta.model or next((e.model for e in document.entries if e.model), None)
    provider = meta.provider or next((e.provider for e in document.entries if e.provider), None)
    domain = config.domain(meta.source, project)
    attr = SessionAttribution(
        meta.source,
        model,
        provider,
        config.locality(provider, model),
        project,
        domain,
        None,
        meta.created_at,
        interactive,
        domain is None,
        unmapped,
    )
    return _apply_registrations(attr, registrations, config)


def _apply_registrations(
    attr: SessionAttribution,
    registrations: list[dict],
    config: Config,
) -> SessionAttribution:
    project, workflow, quotes = attr.project, attr.workflow, set(attr.user_quote_entries)
    for registration in registrations:
        project = registration["project"] or project
        workflow = registration["workflow"] or workflow
        quotes.update(json.loads(registration["user_quote_entries"]))
    domain = config.domain(attr.harness, project)
    return replace(
        attr,
        project=project,
        domain=domain,
        workflow=workflow,
        interactive=attr.interactive and not registrations,
        unmapped_domain=domain is None,
        user_quote_entries=frozenset(quotes),
    )


def reader_document(store: Store, selected: SelectedSession, config: Config) -> SessionDocument:
    """Call immediately before each model invocation, including later parts of a session."""
    from dreaming.knowledge import blocked_call_refs

    if any(s["forgotten"] for s in _copy_states(store, selected.copies)):
        raise SessionError("forgotten", "Session is forgotten")
    blocked = blocked_call_refs(store)
    if any(m.id in blocked or m.thread_id in blocked for m in selected.copies):
        raise SessionError("forgotten_input", "Dream session contains forgotten evidence")
    attr = attribute_session(
        selected.document, store, config, copies=selected.copies, subject=selected.subject
    )
    entries = []
    for index, entry in enumerate(selected.document.entries):
        if entry.unattributed:
            continue
        role = entry.role
        quoted = f"entry:{index}" in attr.user_quote_entries or entry.id in attr.user_quote_entries
        if role == "user" and not attr.interactive and not quoted:
            role = "tool"
        entries.append(
            replace(
                entry,
                role=role,
                model=entry.model or attr.model,
                provider=entry.provider or attr.provider,
                source_index=index,
            )
        )
    return replace(selected.document, entries=tuple(entries), warnings=(), unattributed_summary=())


def reader_parts(
    store: Store,
    selected: SelectedSession,
    config: Config,
    *,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
) -> tuple[SessionPart, ...]:
    document = reader_document(store, selected, config)
    attr = attribute_session(
        selected.document, store, config, copies=selected.copies, subject=selected.subject
    )
    context = min(config.backends[name].context_tokens for name in config.roles["reader"])
    return split_session(
        document,
        context_tokens=context,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        start_entry=selected.start_entry,
        subject=selected.subject,
        attribution=attr,
    )


def register_session(
    store: Store,
    *,
    tool: str,
    run_ref: str,
    source: str | None = None,
    session_id: str | None = None,
    correlation_id: str | None = None,
    workflow: str | None = None,
    project: str | None = None,
    user_quote_entries: tuple[str, ...] = (),
    document_path: Path | None = None,
) -> bool:
    """Register identifiers or an apsis shared-json file; repeated details change nothing."""
    from dreaming.knowledge import queue_reassessment

    metadata = SessionMetadata(source=source, id=session_id, thread_id=correlation_id)
    if document_path is not None:
        document_path = Path(document_path).expanduser().resolve()
        try:
            doc = parse_document(document_path.read_text())
        except OSError as exc:
            raise SessionError("invalid_document", "Cannot read registered file") from exc
        if not isinstance(doc, SessionDocument):
            raise SessionError("invalid_document", "Expected a session file")
        if (source and source != doc.metadata.source) or (
            session_id and session_id != doc.metadata.id
        ):
            raise SessionError("invalid_session", "Registered file identity does not match")
        metadata = doc.metadata
        source, session_id = metadata.source, metadata.id
        if correlation_id and metadata.thread_id and correlation_id != metadata.thread_id:
            raise SessionError("invalid_session", "Correlation identifier does not match")
        correlation_id = correlation_id or metadata.thread_id
    if (
        any(not isinstance(v, str) or not v for v in (tool, run_ref, source, session_id))
        or any(
            v is not None and (not isinstance(v, str) or not v)
            for v in (correlation_id, workflow, project)
        )
        or not isinstance(user_quote_entries, (tuple, list, set, frozenset))
        or any(not isinstance(v, str) or not v for v in user_quote_entries)
    ):
        raise SessionError("invalid_session", "Invalid registration fields")
    metadata = replace(metadata, thread_id=correlation_id or metadata.thread_id)
    metadata = replace(metadata, project=canonical_project(metadata.project))
    quotes = json.dumps(sorted(set(user_quote_entries)))
    project = canonical_project(project)
    with store.transaction() as conn:
        states = _copy_states(store, (metadata,))
        if any(s["forgotten"] for s in states):
            raise SessionError("forgotten", f"{source}/{session_id} is forgotten")
        existing = conn.execute(
            "SELECT correlation_id,workflow,project,user_quote_entries FROM registrations "
            "WHERE tool=? AND run_ref=? AND source=? AND session_id=?",
            (tool, run_ref, source, session_id),
        ).fetchone()
        stored_path = next(
            (
                s.get("document_path")
                for s in states
                if (s["source"], s["session_id"]) == (source, session_id)
            ),
            None,
        )
        values = (correlation_id, workflow, project, quotes)
        if existing == values and (document_path is None or stored_path == str(document_path)):
            return False
        threads = {s["thread_id"] for s in states if s["thread_id"]}
        if correlation_id and threads - {correlation_id}:
            raise SessionError("registration_conflict", "Session already has another correlation")
        _remember(conn, metadata)
        conn.execute(
            "INSERT INTO registrations(tool,run_ref,source,session_id,correlation_id,workflow,"
            "project,user_quote_entries) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "correlation_id=excluded.correlation_id,workflow=excluded.workflow,"
            "project=excluded.project,user_quote_entries=excluded.user_quote_entries",
            (tool, run_ref, source, session_id, *values),
        )
        if document_path is not None:
            conn.execute(
                "UPDATE sessions_seen SET document_path=? WHERE source=? AND session_id=?",
                (str(document_path), source, session_id),
            )
        affected = []
        registrations = _registrations(store, (metadata,))
        quote_ids = {entry for r in registrations for entry in json.loads(r["user_quote_entries"])}
        for state in _copy_states(store, (metadata,)):
            key = (state["source"], state["session_id"])
            conn.execute(
                "UPDATE sessions_seen SET interactive=0,workflow=COALESCE(?,workflow),"
                "project=COALESCE(?,project) WHERE source=? AND session_id=?",
                (workflow, project, *key),
            )
            for oid, entry_id, location, kind in conn.execute(
                "SELECT id,entry_id,source_location,kind FROM observations "
                "WHERE source=? AND session_id=?",
                key,
            ).fetchall():
                affected.append(oid)
                if kind in {"user_statement", "tool_prompt"}:
                    new_kind = (
                        "user_statement"
                        if entry_id in quote_ids or location in quote_ids
                        else "tool_prompt"
                    )
                    conn.execute(
                        "UPDATE observations SET kind=? WHERE id=? AND kind<>?",
                        (new_kind, oid, new_kind),
                    )
        queue_reassessment(conn, set(affected))
    return True

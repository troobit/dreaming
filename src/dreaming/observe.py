"""Observations: the reader's quotes checked, redacted and stored (2.9, 6.1-6.3,
13.1), the Read steps that produce them (4.3, 6.6) and the reader trial (5.6).

The program, not the model, decides what is kept. A quote must appear in the
entry it cites (whitespace-normalised) before it is redacted; a user statement
must quote an entry presented to the reader as the user's; an outcome counts
as confirmed only when the entry it cites is the user's or a check's, never the
assistant's own claim. Whatever fails is recorded with the reason (7.5).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dreaming import config, knowledge, roles, sessions, steps
from dreaming.backends import Backend, BackendError, estimate_tokens
from dreaming.errors import DreamingError, Rejected
from dreaming.redact import redact
from dreaming.run import Context, Outcome
from dreaming.schema import validate
from dreaming.steps import NewStep, Step

REVIEW_KEY = "review"
CHECK_BLOCKS = ("tool_result",)


def normalise(text: str) -> str:
    """Whitespace collapsed to single spaces, as quotes are compared (6.2)."""
    return " ".join(text.split())


def verify(quote: str, text: str) -> bool:
    """Whether `quote` appears in `text`, ignoring differences in whitespace."""
    wanted = normalise(quote)
    return bool(wanted) and wanted in normalise(text)


def normalise_key(key: str, lens: str) -> str:
    """A pattern key compared across sessions: lower case, single spaces and no
    leading lens id (design, Review and knowledge)."""
    key = normalise(key).lower()
    return re.sub(rf"^{re.escape(lens.lower())}\s*[:/-]\s*", "", key)


def evidence_id(*parts: object) -> str:
    """A stable identifier from the parts that make the evidence unique, so the
    same observation always gets the same id (6.3)."""
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode())
    return f"ev-{digest.hexdigest()[:16]}"


# The gate (2.9, 6.2, 13.1)


@dataclass(frozen=True)
class SourceEntry:
    """One entry as observations are checked against it."""

    location: str
    role: str  # as presented to the reader (2.9)
    text: str  # the whole entry, before any cut to fit a part
    check: bool  # holds a tool's result, so it can show a check's outcome
    date: str | None


def source_entries(session: sessions.SelectedSession) -> dict[str, SourceEntry]:
    started = (session.attribution.started_at or "")[:10] or None
    result = {}
    for entry in session.entries():
        when = sessions.parse_time(entry.timestamp)
        result[entry.location] = SourceEntry(
            location=entry.location,
            role=sessions.presented_role(entry, session.attribution),
            text=sessions.entry_text(entry),
            check=entry.role == "tool"
            or any(block.get("type") in CHECK_BLOCKS for block in entry.content),
            date=when.date().isoformat() if when else started,
        )
    return result


@dataclass(frozen=True)
class Observation:
    """A reader observation that passed the gate, its quote redacted."""

    entry: SourceEntry
    quote: str
    kind: str
    modality: str
    outcome: str
    outcome_entry: str | None
    lens: str
    lens_version: int
    pattern_key: str


def _outcome(
    raw: Mapping, entries: Mapping[str, SourceEntry]
) -> tuple[str, str | None]:
    """The outcome and the entry showing it, or `unknown` when that entry is not
    the user's or a check's (13.1)."""
    outcome, shown_by = raw["outcome"], raw["outcome_entry_id"]
    entry = entries.get(shown_by) if shown_by else None
    if outcome == "unknown" or entry is None:
        return "unknown", None
    by_user = entry.role == "user"
    allowed = {
        "user_confirmed": by_user,
        "check_confirmed": entry.check,
        "failed": by_user or entry.check,
    }
    return (outcome, shown_by) if allowed.get(outcome) else ("unknown", None)


def gate(
    raw: Mapping, entries: Mapping[str, SourceEntry], lenses: Mapping[str, int]
) -> Observation:
    """The observation to store, or `Rejected` with the reason.

    `entries` are those of the part the reader saw; `lenses` the lens ids it was
    asked to read under, with their versions.
    """
    entry = entries.get(raw["entry_id"])
    if entry is None:
        raise Rejected(f"entry {raw['entry_id']} is not in the part read")
    if raw["lens"] not in lenses:
        raise Rejected(f"lens {raw['lens']} was not asked for")
    if not verify(raw["quote"], entry.text):
        raise Rejected(f"the quote does not match entry {entry.location}")
    if raw["kind"] == knowledge.USER_STATEMENT and entry.role != "user":
        raise Rejected(
            f"a user statement quotes entry {entry.location}, which is attributed "
            f"to the {entry.role} (2.9)"
        )
    key = normalise_key(raw["pattern_key"], raw["lens"])
    if not key:
        raise Rejected("the observation has no pattern key")
    outcome, shown_by = _outcome(raw, entries)
    return Observation(
        entry=entry,
        quote=redact(normalise(raw["quote"])),
        kind=raw["kind"],
        modality=raw["modality"],
        outcome=outcome,
        outcome_entry=shown_by,
        lens=raw["lens"],
        lens_version=lenses[raw["lens"]],
        pattern_key=key,
    )


def store(
    conn: sqlite3.Connection,
    run_id: str | None,
    session: sessions.SelectedSession,
    obs: Observation,
    *,
    backend: str | None,
    model: str | None,
) -> str:
    """Store a gated observation and return its evidence id. Storing it again,
    as reading the session again under unchanged lens versions does, adds
    nothing (6.3)."""
    a = session.attribution
    identifier = evidence_id(
        session.source,
        session.id,
        obs.entry.location,
        obs.lens,
        obs.lens_version,
        obs.quote,
    )
    conn.execute(
        "INSERT OR IGNORE INTO observations (evidence_id, run, source, session_id,"
        " harness, entry_id, location, representation_version, quote, kind,"
        " modality, outcome, outcome_evidence, lens, lens_version, pattern_key,"
        " project, domain, observed_on, backend, model, subject)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            identifier,
            run_id,
            session.source,
            session.id,
            a.harness,
            obs.entry.location,
            obs.entry.location,
            sessions.SCHEMA_VERSION,
            obs.quote,
            obs.kind,
            obs.modality,
            obs.outcome,
            obs.outcome_entry,
            obs.lens,
            obs.lens_version,
            obs.pattern_key,
            a.project,
            a.domain,
            obs.entry.date,
            backend,
            model,
            a.subject,
        ),
    )
    return identifier


def rejection_detail(raw: Mapping, quote_chars: int) -> dict:
    """What is kept of a rejected observation: never the raw quote (6.2)."""
    return {
        "entry_id": raw.get("entry_id"),
        "lens": raw.get("lens"),
        "kind": raw.get("kind"),
        "quote": redact(normalise(str(raw.get("quote", ""))))[:quote_chars],
    }


# Read steps (4.3, 6.6)


def read_key(source: str, session_id: str) -> str:
    digest = hashlib.sha256(f"{source}\0{session_id}".encode()).hexdigest()
    return f"read:{digest[:12]}"


def _session_ref(session: sessions.SelectedSession) -> dict:
    attribution = asdict(session.attribution)
    attribution["quote_entries"] = sorted(attribution["quote_entries"])
    return {
        "source": session.source,
        "id": session.id,
        "start": session.start,
        "copies": [asdict(row) for row in session.copies],
        "attribution": attribution,
    }


def call_input_ref(copies: Sequence[Mapping]) -> str:
    """A call's `input_ref`: the sessions whose content its input held, so a later
    dream can exclude the call's own session once one of them is forgotten
    (2.11)."""
    found = sorted({(copy["source"], copy["id"]) for copy in copies})
    return json.dumps({"sessions": [list(pair) for pair in found]})


def evidence_input_ref(conn: sqlite3.Connection, evidence_ids: Sequence[str]) -> str:
    """A call's `input_ref` when its input holds evidence: the sessions those
    observations came from (2.11)."""
    rows = conn.execute(
        "SELECT DISTINCT source, session_id AS id FROM observations"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(evidence_ids)),),
    )
    return call_input_ref([dict(row) for row in rows])


def _forgotten(conn: sqlite3.Connection, copies: Sequence[Mapping]) -> bool:
    """Whether any copy of the session is forgotten now (2.11)."""
    for copy in copies:
        row = conn.execute(
            "SELECT 1 FROM sessions_seen WHERE forgotten = 1"
            " AND ((source = ? AND session_id = ?) OR thread_id = ?)",
            (copy["source"], copy["id"], copy.get("thread_id") or copy["id"]),
        ).fetchone()
        if row is not None:
            return True
    return False


@dataclass
class Answer:
    """One reader call's output and the part it saw."""

    entries: dict[str, SourceEntry]
    output: dict
    backend: str | None
    model: str | None


@dataclass
class ReadResult:
    ref: dict
    session: sessions.SelectedSession | None = None
    lenses: dict[str, int] = field(default_factory=dict)
    answers: list[Answer] = field(default_factory=list)
    skipped: str | None = None
    forgotten: bool = False
    quote_chars: int = 0


class ReadPhase:
    """One Read step per selected session: each part of the session is one
    reader call, and the step's results are its gated observations."""

    def __init__(self, instructions: roles.InstructionSet, apsis: sessions.Apsis):
        self.instructions = instructions
        self.apsis = apsis

    def plan(self, ctx: Context) -> list[NewStep]:
        """A new run's steps: one Read step per selected session or session that
        could not be parsed (2.7), and the Review step after all of them."""
        chosen = sessions.select_sessions(
            ctx.conn,
            self.apsis,
            ctx.cfg,
            run_request(ctx.conn, ctx.run_id),
            home=ctx.home,
            run_id=ctx.run_id,
            now=ctx.clock.now(),
        )
        planned = [
            NewStep(read_key(s.source, s.id), "read", input_ref=json.dumps(ref))
            for s, ref in ((s, _session_ref(s)) for s in chosen.sessions)
        ]
        planned += [
            NewStep(
                read_key(s.source, s.id),
                "read",
                input_ref=json.dumps(
                    {"source": s.source, "id": s.id, "skipped": s.reason}
                ),
            )
            for s in chosen.skipped
        ]
        keys = tuple(dict.fromkeys(step.key for step in planned))
        return [*planned, NewStep(REVIEW_KEY, "review", depends_on=keys)]

    def _open(self, ref: dict) -> sessions.SessionDocument:
        if ref["source"] == sessions.FILE_SOURCE:
            return sessions.read_session_file(Path(ref["id"]))
        return self.apsis.session(ref["source"], ref["id"])

    def prepare(self, ctx: Context, step: Step) -> ReadResult:
        ref = json.loads(step.input_ref)
        if "skipped" in ref:
            return ReadResult(ref, skipped=ref["skipped"])
        if _forgotten(ctx.conn, ref["copies"]):
            return ReadResult(ref, forgotten=True)
        try:
            document = self._open(ref)
        except DreamingError as exc:
            return ReadResult(ref, skipped=exc.message)
        attribution = dict(ref["attribution"])
        attribution["quote_entries"] = frozenset(attribution["quote_entries"])
        session = sessions.SelectedSession(
            source=ref["source"],
            id=ref["id"],
            document=document,
            copies=[sessions.ListingRow(**row) for row in ref["copies"]],
            start=ref["start"],
            attribution=sessions.Attribution(**attribution),
        )
        scope = run_request(ctx.conn, ctx.run_id).scope
        lenses = roles.reader_lenses(
            sessions.scope_lenses(self.instructions.lenses, scope, ctx.cfg),
            session.attribution,
        )
        result = ReadResult(
            ref,
            session,
            {lens.id: lens.version for lens in lenses},
            quote_chars=ctx.cfg["thresholds"]["quote_chars"],
        )
        if not lenses:
            return result
        reader = self.instructions.roles["reader"]
        parts = split(ctx.cfg, reader, session, lenses)
        input_ref = call_input_ref(ref["copies"])
        entries = source_entries(session)
        for number, (info, part) in enumerate(parts, start=1):
            # Forgetting takes effect before the next call (2.11).
            if number > 1 and _forgotten(ctx.conn, ref["copies"]):
                return ReadResult(ref, forgotten=True)
            data = roles.reader_input(info, lenses, part)
            output = ctx.call(
                "reader",
                reader.prompt(data),
                reader.schema,
                part=f"p{number}",
                input_ref=input_ref,
            )
            backend, model = steps.answered_by(
                ctx.conn, ctx.run_id, steps.call_key(step.key, f"p{number}")
            )
            seen = {entry.location: entries[entry.location] for entry in part}
            result.answers.append(Answer(seen, output, backend, model))
        return result

    def apply(self, conn: sqlite3.Connection, step: Step, result: ReadResult):
        ref = result.ref
        if result.forgotten:
            return Outcome("skipped", "the session was forgotten during the dream")
        if result.skipped is not None:
            sessions.record_skipped(
                conn,
                step.run,
                sessions.Skipped(ref["source"], ref["id"], result.skipped),
            )
            return Outcome("skipped", result.skipped)
        for answer in result.answers:
            for raw in answer.output["observations"]:
                try:
                    obs = gate(raw, answer.entries, result.lenses)
                except Rejected as rejected:
                    steps.record_rejection(
                        conn,
                        step.run,
                        step.key,
                        "observation",
                        f"{ref['source']}:{ref['id']}#{raw.get('entry_id')}",
                        rejected.reason,
                        rejection_detail(raw, result.quote_chars),
                    )
                    continue
                store(
                    conn,
                    step.run,
                    result.session,
                    obs,
                    backend=answer.backend,
                    model=answer.model,
                )
        sessions.record_session(
            conn, step.run, result.session, lens_versions=result.lenses
        )
        return Outcome()

    def failed(self, conn: sqlite3.Connection, step: Step, reason: str) -> None:
        """A session whose reading failed twice is recorded as failed, listed in
        the report and not read again unless selected explicitly (4.7)."""
        ref = json.loads(step.input_ref)
        sessions.record_skipped(
            conn,
            step.run,
            sessions.Skipped(ref["source"], ref["id"], reason),
            state="failed",
        )


def run_request(conn: sqlite3.Connection, run_id: str) -> sessions.SelectionRequest:
    """What the run was asked to read, including its scope (2.8)."""
    row = conn.execute("SELECT selection FROM runs WHERE id = ?", (run_id,))
    return sessions.SelectionRequest.from_dict(json.loads(row.fetchone()[0]))


def split(
    cfg: dict,
    reader: roles.Role,
    session: sessions.SelectedSession,
    lenses: Sequence[config.Lens],
) -> list[tuple[dict, list[sessions.ReadEntry]]]:
    """The session's parts, each with the session details the reader sees (2.6)."""
    info = {
        "harness": session.attribution.harness,
        "project": session.attribution.project,
        "started_at": session.attribution.started_at,
    }
    room = roles.reader_room(cfg, reader, {**info, "part": "999/999"}, lenses)
    parts = sessions.split_entries(session.read_entries(), room)
    if len(parts) == 1:
        return [(info, parts[0])]
    return [
        ({**info, "part": f"{number}/{len(parts)}"}, part)
        for number, part in enumerate(parts, start=1)
    ]


# The reader trial (5.6, design Risks and Assumptions)


def trial_reader(
    chosen: Sequence[sessions.SelectedSession],
    backend: Backend,
    instructions: roles.InstructionSet,
    cfg: dict,
) -> dict:
    """Run the reader on recorded sessions with one backend, and measure how
    often its output matches the schema and its quotes match their entries.

    Each call is made once, with no retry or fallback, and nothing is stored.
    """
    reader = instructions.roles["reader"]
    totals = {
        "calls": 0,
        "failed_calls": 0,
        "schema_valid": 0,
        "observations": 0,
        "quote_matches": 0,
        "accepted": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "usage_estimated": False,
    }
    errors: list[str] = []
    for session in chosen:
        lenses = roles.reader_lenses(instructions.lenses, session.attribution)
        if not lenses:
            continue
        entries = source_entries(session)
        versions = {lens.id: lens.version for lens in lenses}
        for info, part in split(cfg, reader, session, lenses):
            prompt = reader.prompt(roles.reader_input(info, lenses, part))
            totals["calls"] += 1
            try:
                result = backend.call("reader", prompt, reader.schema, [])
            except BackendError as exc:
                totals["failed_calls"] += 1
                errors.append(exc.message)
                continue
            if result.tokens_in is None or result.tokens_out is None:
                totals["usage_estimated"] = True
            totals["tokens_in"] += (
                estimate_tokens(prompt)
                if result.tokens_in is None
                else result.tokens_in
            )
            totals["tokens_out"] += (
                estimate_tokens(json.dumps(result.output))
                if result.tokens_out is None
                else result.tokens_out
            )
            if validate(result.output, reader.schema):
                continue
            totals["schema_valid"] += 1
            seen = {entry.location: entries[entry.location] for entry in part}
            for raw in result.output["observations"]:
                totals["observations"] += 1
                entry = seen.get(raw["entry_id"])
                if entry is not None and verify(raw["quote"], entry.text):
                    totals["quote_matches"] += 1
                try:
                    gate(raw, seen, versions)
                except Rejected:
                    continue
                totals["accepted"] += 1

    def rate(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None

    answered = totals["calls"] - totals["failed_calls"]
    return {
        "backend": backend.name,
        "sessions": len(chosen),
        **totals,
        "schema_valid_rate": rate(totals["schema_valid"], answered),
        "quote_match_rate": rate(totals["quote_matches"], totals["observations"]),
        "errors": errors,
    }

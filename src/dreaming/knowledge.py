"""Live evidence views and reversible session exclusion.

Consumers must use these views immediately before a role call, retrieval, ranking
or adoption. Retained rows and statuses are never erased by session exclusion.
Knowledge revision and promotion operations are implemented by later phases.
"""

import json
import math
from datetime import UTC, datetime

from dreaming.config import Config
from dreaming.sessions import SessionError, SessionMetadata, _copy_states, _timestamp
from dreaming.store import Store


def _rows(cursor) -> list[dict]:
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def queue_reassessment(conn, evidence_ids: set[str]) -> None:
    """The reviewer consumes this durable queue by scope; history remains unchanged."""
    if not evidence_ids:
        return
    for item in _rows(conn.execute("SELECT * FROM knowledge_items")):
        references = set(json.loads(item["supporting_evidence"])) | set(
            json.loads(item["contradicting_evidence"])
        )
        if references & evidence_ids:
            conn.execute(
                "UPDATE knowledge_items SET pending_reassessment=1 "
                "WHERE id=? AND pending_reassessment=0",
                (item["id"],),
            )


def _set_forgotten(store: Store, source: str, session_id: str, forgotten: bool) -> bool:
    with store.transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sessions_seen WHERE source=? AND session_id=?", (source, session_id)
        ).fetchone()
        if not exists:
            raise SessionError("not_found", f"Unknown session {source}/{session_id}")
        states = _copy_states(store, (SessionMetadata(source=source, id=session_id),))
        if all(bool(s["forgotten"]) == forgotten for s in states):
            return False
        previously_blocked = blocked_call_refs(store)
        evidence_ids = set()
        for state in states:
            key = (state["source"], state["session_id"])
            conn.execute(
                "UPDATE sessions_seen SET forgotten=? WHERE source=? AND session_id=? "
                "AND forgotten<>?",
                (int(forgotten), *key, int(forgotten)),
            )
            evidence_ids.update(
                row[0]
                for row in conn.execute(
                    "SELECT id FROM observations WHERE source=? AND session_id=?", key
                )
            )
        # Derived dream sessions and their observations are excluded by the same live view.
        blocked = previously_blocked | blocked_call_refs(store)
        for row in conn.execute(
            "SELECT o.id,s.session_id,s.thread_id FROM observations o "
            "JOIN sessions_seen s USING(source,session_id)"
        ):
            if row[1] in blocked or row[2] in blocked:
                evidence_ids.add(row[0])
        queue_reassessment(conn, evidence_ids)
    return True


def forget_session(store: Store, source: str, session_id: str) -> bool:
    return _set_forgotten(store, source, session_id, True)


def restore_session(store: Store, source: str, session_id: str) -> bool:
    return _set_forgotten(store, source, session_id, False)


def record_call_inputs(store: Store, call_id: str, sessions: tuple[tuple[str, str], ...]) -> None:
    """Record transitive input lineage before dispatch, alongside the existing calls row."""
    with store.transaction() as conn:
        for source, sid in sessions:
            states = _copy_states(store, (SessionMetadata(source=source, id=sid),))
            if not states:
                raise SessionError("not_found", f"Unknown input session {source}/{sid}")
            blocked = blocked_call_refs(store)
            if any(
                s["forgotten"] or s["session_id"] in blocked or s["thread_id"] in blocked
                for s in states
            ):
                raise SessionError("forgotten", f"Input session {source}/{sid} is forgotten")
            conn.execute(
                "INSERT OR IGNORE INTO session_inputs(call_id,source,session_id) VALUES (?,?,?)",
                (call_id, source, sid),
            )


def blocked_call_refs(store: Store) -> set[str]:
    """Exclude call transcripts AND run summaries that contain forgotten inputs."""
    sessions = _rows(store.connection.execute("SELECT * FROM sessions_seen"))
    forgotten_threads = {s["thread_id"] for s in sessions if s["forgotten"] and s["thread_id"]}
    blocked_keys = {
        (s["source"], s["session_id"])
        for s in sessions
        if s["forgotten"] or s["thread_id"] in forgotten_threads
    }
    calls = _rows(
        store.connection.execute(
            "SELECT c.run_id,c.session_ref,i.source,i.session_id FROM calls c "
            "JOIN session_inputs i ON i.call_id=c.id"
        )
    )
    refs = set()
    while True:
        previous = (len(refs), len(blocked_keys))
        for call in calls:
            if (call["source"], call["session_id"]) in blocked_keys:
                refs.add(call["run_id"])
                if call["session_ref"]:
                    refs.add(call["session_ref"])
        blocked_keys.update(
            (s["source"], s["session_id"])
            for s in sessions
            if s["session_id"] in refs or s["thread_id"] in refs
        )
        if previous == (len(refs), len(blocked_keys)):
            return refs


def available_run_records(store: Store) -> list[dict]:
    blocked = blocked_call_refs(store)
    return [
        row
        for row in _rows(store.connection.execute("SELECT * FROM runs ORDER BY id"))
        if row["id"] not in blocked
    ]


def available_observations(
    store: Store,
    *,
    pattern_id: str | None = None,
    evidence_ids: list[str] | None = None,
) -> list[dict]:
    """Shared support/retrieval/replay view, counting each interaction separately below."""
    blocked = blocked_call_refs(store)
    forgotten_threads = {
        row[0]
        for row in store.connection.execute(
            "SELECT thread_id FROM sessions_seen WHERE forgotten=1 AND thread_id IS NOT NULL"
        )
    }
    allowed = set(evidence_ids) if evidence_ids is not None else None
    rows = _rows(
        store.connection.execute(
            "SELECT o.*,s.thread_id,s.interactive,s.workflow FROM observations o "
            "JOIN sessions_seen s USING(source,session_id) "
            "WHERE o.excluded=0 AND s.forgotten=0 ORDER BY o.observed_at,o.id"
        )
    )
    return [
        row
        for row in rows
        if row["session_id"] not in blocked
        and row["thread_id"] not in blocked
        and row["thread_id"] not in forgotten_threads
        and (pattern_id is None or row["pattern_id"] == pattern_id)
        and (allowed is None or row["id"] in allowed)
    ]


def interaction_key(observation: dict) -> tuple:
    return (
        ("thread", observation["thread_id"])
        if observation.get("thread_id")
        else (observation["source"], observation["session_id"])
    )


def pattern_statistics(store: Store, pattern_id: str) -> dict:
    observations = available_observations(store, pattern_id=pattern_id)
    return {
        "observations": len(observations),
        "sessions": len({interaction_key(row) for row in observations}),
        "projects": len({row["project"] for row in observations if row["project"]}),
        "latest_support_at": max(
            (row["observed_at"] for row in observations if row["observed_at"]), default=None
        ),
    }


def rank_patterns(store: Store, config: Config, *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    ranked = []
    thresholds = config.thresholds
    for (pid,) in store.connection.execute("SELECT id FROM patterns ORDER BY id"):
        stats = pattern_statistics(store, pid)
        if not stats["sessions"]:
            continue
        latest = _timestamp(stats["latest_support_at"])
        recency = (
            math.pow(2, -max(0, (now - latest).total_seconds() / 86400) / thresholds.half_life_days)
            if latest
            else 0
        )
        score = (
            thresholds.w_sessions * stats["sessions"]
            + thresholds.w_recency * recency
            + thresholds.w_projects * stats["projects"]
        )
        ranked.append({"id": pid, **stats, "recency": recency, "score": score})
    return sorted(ranked, key=lambda row: (-row["score"], row["id"]))


def _user_statement(store: Store, observation: dict) -> bool:
    if observation["kind"] != "user_statement":
        return False
    if observation["interactive"]:
        return True
    states = _copy_states(
        store, (SessionMetadata(source=observation["source"], id=observation["session_id"]),)
    )
    keys = {(s["source"], s["session_id"]) for s in states}
    for source, sid, quotes in store.connection.execute(
        "SELECT source,session_id,user_quote_entries FROM registrations"
    ):
        if (source, sid) in keys and {
            observation["entry_id"],
            observation["source_location"],
        } & set(json.loads(quotes)):
            return True
    return False


def _support_sufficient(
    store: Store,
    evidence: list[str],
    config: Config,
    *,
    kind: str = "",
    scope: dict | None = None,
) -> bool:
    observations = available_observations(store, evidence_ids=evidence)
    if scope:
        observations = [
            row
            for row in observations
            if all(
                not scope.get(dimension) or row[field] in scope[dimension]
                for dimension, field in (
                    ("domains", "domain"),
                    ("projects", "project"),
                    ("workflows", "workflow"),
                )
            )
        ]
    if not (
        any(_user_statement(store, row) for row in observations)
        or len({interaction_key(row) for row in observations}) >= config.thresholds.min_sessions
    ):
        return False
    if kind == "approach_worked":
        return any(row["outcome"] in {"user-confirmed", "check-confirmed"} for row in observations)
    return True


def knowledge_for_use(store: Store, config: Config) -> list[dict]:
    """Current active items with live evidence. Scope/limits belong to the later API."""
    result = []
    stored_observations = {
        row[0] for row in store.connection.execute("SELECT id FROM observations")
    }
    file_ids = {row[0] for row in store.connection.execute("SELECT id FROM file_findings")}
    for item in _rows(
        store.connection.execute("SELECT * FROM knowledge_items WHERE status='active'")
    ):
        evidence = json.loads(item["supporting_evidence"])
        available = available_observations(store, evidence_ids=evidence)
        # Late registration alone marks pending, without withdrawing retained knowledge (2.10).
        # Forgetting or excluding evidence additionally enforces immediate withholding (2.11).
        lost_support = bool((set(evidence) & stored_observations) - {r["id"] for r in available})
        if lost_support and not _support_sufficient(
            store,
            evidence,
            config,
            kind=item["kind"],
            scope=json.loads(item["scope"]),
        ):
            continue
        item["supporting_evidence"] = [row["id"] for row in available] + sorted(
            file_ids & set(evidence)
        )
        contradicting = json.loads(item["contradicting_evidence"])
        item["contradicting_evidence"] = [
            row["id"] for row in available_observations(store, evidence_ids=contradicting)
        ] + sorted(file_ids & set(contradicting))
        item["pending_reassessment"] = bool(item["pending_reassessment"])
        result.append(item)
    return result


def change_is_eligible(store: Store, change_id: str, config: Config) -> bool:
    cursor = store.connection.execute("SELECT * FROM changes WHERE id=?", (change_id,))
    rows = _rows(cursor)
    if not rows or rows[0]["status"] not in {"proposed", "waiting", "withheld"}:
        return False
    change = rows[0]
    evidence = json.loads(change["evidence"])
    file_ids = {r[0] for r in store.connection.execute("SELECT id FROM file_findings")}
    worked_claim = store.connection.execute(
        "SELECT 1 FROM knowledge_items WHERE pattern_id=? AND kind='approach_worked' "
        "AND status='active' LIMIT 1",
        (change["pattern_id"],),
    ).fetchone()
    return bool(file_ids & set(evidence)) or _support_sufficient(
        store,
        evidence,
        config,
        kind="approach_worked" if worked_claim else "",
    )

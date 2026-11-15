"""Live evidence views and reversible session exclusion.

Consumers must use these views immediately before a role call, retrieval, ranking
or adoption. Retained rows and statuses are never erased by session exclusion.
Revisions are append-only and operations guard scope, provenance and versions.
"""

import json
import math
from datetime import UTC, datetime

from dreaming.config import Config
from dreaming.observe import canonical, identifier, normalise
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
    for row in rows:
        if row["outcome"] == "user-confirmed" and not row["interactive"]:
            keys = {
                (s["source"], s["session_id"])
                for s in _copy_states(
                    store, (SessionMetadata(source=row["source"], id=row["session_id"]),)
                )
            }
            quoted = {
                entry
                for source, sid, entries in store.connection.execute(
                    "SELECT source,session_id,user_quote_entries FROM registrations"
                )
                if (source, sid) in keys
                for entry in json.loads(entries)
            }
            if not set(json.loads(row["outcome_evidence"])) & quoted:
                row["outcome"] = "unknown"
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


def available_file_findings(store: Store, evidence_ids=None) -> list[dict]:
    allowed = set(evidence_ids) if evidence_ids is not None else None
    rows = _rows(
        store.connection.execute(
            "SELECT f.*,p.key AS pattern_key FROM file_findings f "
            "LEFT JOIN patterns p ON p.id=f.pattern_id WHERE f.availability='available' "
            "ORDER BY f.observed_at,f.id"
        )
    )
    return [row for row in rows if allowed is None or row["id"] in allowed]


def pattern_statistics(store: Store, pattern_id: str) -> dict:
    observations = [
        row for row in available_observations(store, pattern_id=pattern_id) if not row["imported"]
    ]
    return {
        "observations": len(observations),
        "sessions": len({interaction_key(row) for row in observations}),
        "projects": len({row["project"] for row in observations if row["project"]}),
        "latest_support_at": latest_support(observations),
    }


def latest_support(rows):
    dates = [row["observed_at"] for row in rows if _timestamp(row.get("observed_at"))]
    return max(dates, key=_timestamp, default=None)


def rank_patterns(
    store: Store,
    config: Config,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> list[dict]:
    now = now or datetime.now(UTC)
    ranked = []
    thresholds = config.thresholds
    for pid, last_run in store.connection.execute(
        "SELECT id,last_reviewed_run FROM patterns WHERE merged_into IS NULL ORDER BY id"
    ):
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
        boost = 0
        if run_id is not None:
            anchor = store.connection.execute(
                "SELECT rowid FROM runs WHERE id=?", (last_run,)
            ).fetchone()
            age = store.connection.execute(
                "SELECT count(*) FROM runs WHERE rowid>? AND id<>?",
                (anchor[0] if anchor else 0, run_id),
            ).fetchone()[0]
            boost = age // thresholds.boost_after_dreams
        ranked.append(
            {"id": pid, **stats, "recency": recency, "score": score + boost, "boost": boost}
        )
    return sorted(ranked, key=lambda row: (-row["score"], row["id"]))


def _user_statement(store: Store, observation: dict) -> bool:
    if observation["kind"] != "user_statement":
        return False
    if observation.get("modality", "asserted") != "asserted" or observation.get(
        "observation_type", ""
    ) not in {"", "preference", "correction", "user_statement", "learning_goal"}:
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
            observation.get("source_entry_id"),
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
    observations = [
        row for row in available_observations(store, evidence_ids=evidence) if not row["imported"]
    ]
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
    if kind == "learning":
        observations = [
            row
            for row in observations
            if row["kind"] == "user_statement"
            and row["observation_type"] in {"learning", "learning_goal"}
            and (row["interactive"] or _user_statement(store, row))
        ]
        return (
            promotion_status(observations, config.thresholds.min_sessions, "learning") == "active"
        )
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


def explicit_statement(row: dict) -> bool:
    return (
        not row.get("imported", False)
        and row["kind"] == "user_statement"
        and row["modality"] == "asserted"
        and row.get("observation_type", "")
        in {"", "preference", "correction", "user_statement", "learning_goal"}
    )


def promotion_status(rows: list[dict], min_sessions: int, kind: str) -> str:
    rows = [row for row in rows if not row.get("imported", False)]
    if kind == "learning":
        rows = [
            row
            for row in rows
            if row["kind"] == "user_statement"
            and row.get("observation_type") in {"learning", "learning_goal"}
        ]
    enough = (
        any(explicit_statement(row) for row in rows)
        or len({interaction_key(row) for row in rows}) >= min_sessions
    )
    if kind == "approach_worked":
        enough &= any(row["outcome"] in {"user-confirmed", "check-confirmed"} for row in rows)
    return "active" if enough else "candidate"


def normalise_scope(scope: dict) -> dict:
    if not isinstance(scope, dict) or scope.keys() - {
        "domains",
        "projects",
        "workflows",
        "global",
        "unresolved",
    }:
        raise ValueError("invalid_scope")
    result = {}
    for name in ("domains", "projects", "workflows"):
        values = scope.get(name, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise ValueError("invalid_scope")
        result[name] = sorted(set(values))
    for name in ("global", "unresolved"):
        if type(scope.get(name, False)) is not bool:
            raise ValueError("invalid_scope")
        result[name] = scope.get(name, False)
    if result["global"] and (
        result["unresolved"] or any(result[n] for n in ("domains", "projects", "workflows"))
    ):
        raise ValueError("invalid_scope")
    return result


def in_scope(row: dict, scope: dict) -> bool:
    return all(
        not scope[dimension]
        or (row.get(field) or (row.get("repo") if field == "project" else None)) in scope[dimension]
        for dimension, field in (
            ("domains", "domain"),
            ("projects", "project"),
            ("workflows", "workflow"),
        )
    )


class KnowledgeRules:
    """Validated operations inside the caller's short result transaction."""

    def __init__(
        self, store, config, run_id, *, actor="dream", model=None, feedback_id=None, now=None
    ):
        if actor not in {"dream", "user", "feedback"}:
            raise ValueError("invalid_actor")
        self.store, self.config, self.run_id = store, config, run_id
        self.actor, self.model, self.feedback_id = actor, model, feedback_id
        self.now = now or datetime.now(UTC)
        self.initial_active = {}
        for scope, count in store.connection.execute(
            "SELECT scope,count(*) FROM knowledge_items WHERE status='active' GROUP BY scope"
        ):
            self.initial_active[scope] = count

    def apply(self, conn, operation: dict) -> dict:
        conn.execute("SAVEPOINT knowledge_operation")
        try:
            result = self._apply(conn, operation)
        except (ValueError, KeyError, TypeError) as exc:
            conn.execute("ROLLBACK TO knowledge_operation")
            result = {
                "disposition": "rejected",
                "reason": str(exc) if isinstance(exc, ValueError) else "invalid_operation",
            }
            if result["reason"] == "stale_version":
                for item_id in {
                    operation.get("item_id"),
                    operation.get("target_id"),
                    *operation.get("item_ids", []),
                } - {None}:
                    conn.execute(
                        "UPDATE knowledge_items SET pending_reassessment=1 WHERE id=?", (item_id,)
                    )
        conn.execute("RELEASE knowledge_operation")
        return result

    def _item(self, conn, item_id, version=None):
        rows = _rows(conn.execute("SELECT * FROM knowledge_items WHERE id=?", (item_id,)))
        if not rows:
            raise ValueError("unknown_item")
        item = rows[0]
        if version is not None and (type(version) is not int or version != item["version"]):
            raise ValueError("stale_version")
        return item

    def _dream_pattern(self, conn, pattern_id):
        row = conn.execute("SELECT lens_scope FROM patterns WHERE id=?", (pattern_id,)).fetchone()
        return bool(row and json.loads(row[0]).get("subject") == "dreaming")

    def _protected(self, conn, item):
        if not item["user_resolved"]:
            return False
        if not item["pending_reassessment"]:
            return True
        manual = conn.execute(
            "SELECT 1 FROM knowledge_revisions WHERE item_id=? "
            "AND actor IN ('user','feedback') AND user_resolved=1 LIMIT 1",
            (item["id"],),
        ).fetchone()
        rows = available_observations(
            self.store, evidence_ids=json.loads(item["supporting_evidence"])
        )
        return bool(manual or any(_user_statement(self.store, row) for row in rows))

    def _evidence(self, ids):
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise ValueError("evidence_required")
        rows = available_observations(self.store, evidence_ids=ids)
        if {r["id"] for r in rows} != set(ids):
            raise ValueError("unavailable_evidence")
        return sorted(rows, key=lambda row: (row["observed_at"] or "", row["id"]))

    def _scope(self, proposed, rows):
        scope = normalise_scope(proposed)
        declared = [
            normalise_scope(json.loads(r.get("declared_scope", "{}")))
            for r in rows
            if explicit_statement(r)
        ]
        if scope["global"]:
            if not any(s["global"] for s in declared) and self.actor == "dream":
                raise ValueError("unsupported_global_scope")
            return scope
        if (
            not any(scope[d] for d in ("domains", "projects", "workflows"))
            and not scope["unresolved"]
        ):
            for dimension, field in (
                ("domains", "domain"),
                ("projects", "project"),
                ("workflows", "workflow"),
            ):
                values = sorted({r[field] for r in rows if r.get(field)})
                # Missing source context stays unresolved instead of becoming global.
                if values and all(r.get(field) for r in rows):
                    scope[dimension] = values
            scope["unresolved"] = not any(scope[d] for d in ("domains", "projects", "workflows"))
        for dimension, field in (
            ("domains", "domain"),
            ("projects", "project"),
            ("workflows", "workflow"),
        ):
            known = {r.get(field) for r in rows}
            declared_values = {v for s in declared for v in s[dimension]}
            if set(scope[dimension]) - known - declared_values and self.actor == "dream":
                raise ValueError("unsupported_scope")
        return scope

    def _write(self, conn, old, values):
        if old and all(old.get(key) == value for key, value in values.items()):
            return {"disposition": "unchanged", "item_id": old["id"], "status": old["status"]}
        item_id = (
            old["id"]
            if old
            else identifier(
                "k",
                [
                    values["kind"],
                    values["scope"],
                    normalise(values["summary"]).casefold(),
                    values["pattern_id"],
                ],
            )
        )
        version = old["version"] + 1 if old else 1
        values = {
            **values,
            "version": version,
            "actor": self.actor,
            "model": self.model,
            "run_id": self.run_id,
            "feedback_id": self.feedback_id,
            "base_version": old["version"] if old else None,
        }
        if old:
            conn.execute(
                f"UPDATE knowledge_items SET {','.join(k + '=?' for k in values)} WHERE id=?",
                (*values.values(), item_id),
            )
        else:
            placeholders = ",".join("?" for _ in range(len(values) + 1))
            conn.execute(
                f"INSERT INTO knowledge_items(id,{','.join(values)}) VALUES ({placeholders})",
                (item_id, *values.values()),
            )
        revision = {k: v for k, v in values.items() if k != "pattern_id"}
        revision["created_at"] = self.now.isoformat()
        placeholders = ",".join("?" for _ in range(len(revision) + 1))
        conn.execute(
            f"INSERT INTO knowledge_revisions(item_id,{','.join(revision)}) "
            f"VALUES ({placeholders})",
            (item_id, *revision.values()),
        )
        return {
            "disposition": "applied",
            "item_id": item_id,
            "version": version,
            "status": values["status"],
        }

    def _revise(self, conn, proposed, old=None):
        if proposed["kind"] not in {
            "preference",
            "lesson",
            "workflow",
            "learning",
            "approach_worked",
        }:
            raise ValueError("invalid_kind")
        supporting = self._evidence(proposed["supporting_evidence"])
        contra = (
            self._evidence(proposed["contradicting_evidence"])
            if proposed["contradicting_evidence"]
            else []
        )
        scope = self._scope(proposed["scope"], supporting)
        supporting = [r for r in supporting if in_scope(r, scope)]
        contra = [r for r in contra if in_scope(r, scope)]
        if not supporting:
            raise ValueError("no_support_in_scope")
        pids = {r["pattern_id"] for r in supporting if r["pattern_id"]}
        if len(pids) != 1:
            raise ValueError("single_pattern_required")
        pid = next(iter(pids))
        scope_json = canonical(scope)
        equivalent = _rows(
            conn.execute("SELECT * FROM knowledge_items WHERE scope=? ORDER BY id", (scope_json,))
        )
        if not old:
            for candidate in equivalent:
                if self._dream_pattern(conn, candidate["pattern_id"]) != self._dream_pattern(
                    conn, pid
                ):
                    continue
                if normalise(candidate["summary"]).casefold() == normalise(
                    proposed["summary"]
                ).casefold() or (
                    candidate["pattern_id"] == pid and candidate["kind"] == proposed["kind"]
                ):
                    old = candidate
                    break
        if old:
            if self._dream_pattern(conn, old["pattern_id"]) != self._dream_pattern(conn, pid):
                raise ValueError("dream_evidence_cannot_reinforce_user_knowledge")
        if old and old["status"] in {"dismissed", "superseded"} and self.actor == "dream":
            raise ValueError("dismissed_interpretation")
        correction = any(
            explicit_statement(r) and r["observation_type"] == "correction" for r in supporting
        )
        protected = bool(old and self._protected(conn, old))
        if protected and not correction and self.actor == "dream":
            raise ValueError("user_resolution_protected")
        status = promotion_status(supporting, self.config.thresholds.min_sessions, proposed["kind"])
        if (
            any(r["outcome"] == "failed" for r in supporting)
            and proposed["kind"] == "approach_worked"
        ):
            contra = list(
                {
                    r["id"]: r
                    for r in [*contra, *(r for r in supporting if r["outcome"] == "failed")]
                }.values()
            )
        if contra and not correction and self.actor == "dream":
            status = "contested"
        confidence = proposed["confidence"]
        if (
            confidence not in {"low", "medium", "high"}
            or not proposed["confidence_basis"].strip()
            or not proposed["summary"].strip()
        ):
            raise ValueError("invalid_interpretation")
        if status != "active":
            confidence = "low"
        elif all(r["modality"] != "asserted" for r in supporting) and confidence == "high":
            confidence = "medium"
        values = dict(
            kind=proposed["kind"],
            scope=scope_json,
            status=status,
            summary=normalise(proposed["summary"]),
            confidence=confidence,
            confidence_basis=proposed["confidence_basis"],
            supporting_evidence=canonical(sorted(r["id"] for r in supporting)),
            contradicting_evidence=canonical(sorted(r["id"] for r in contra)),
            latest_support_at=latest_support(supporting),
            lens_id=proposed["lens_id"],
            pattern_id=pid,
            pending_reassessment=0,
            user_resolved=int(correction or self.actor != "dream" or protected),
        )
        return self._write(conn, old, values)

    def _apply(self, conn, operation):
        op = operation["op"]
        if op == "create":
            return self._revise(conn, operation["item"])
        old = self._item(
            conn, operation.get("item_id", operation.get("target_id")), operation["base_version"]
        )
        if op == "revise":
            return self._revise(conn, operation["item"], old)
        if op == "merge_items":
            sources = [
                self._item(conn, iid, operation.get("base_versions", {}).get(iid, -1))
                for iid in sorted(set(operation["item_ids"]))
                if iid != old["id"]
            ]
            if any(
                r["scope"] != old["scope"] or r["status"] == "dismissed" or r["user_resolved"]
                for r in sources
            ):
                raise ValueError("incompatible_merge")
            proposed = {
                k: old[k] for k in ("kind", "summary", "confidence", "confidence_basis", "lens_id")
            }
            proposed["scope"] = json.loads(old["scope"])
            for field in ("supporting_evidence", "contradicting_evidence"):
                proposed[field] = sorted(
                    {eid for r in [old, *sources] for eid in json.loads(r[field])}
                )
            for row in sources:
                result = self._transition(conn, row, "superseded", operation["reason"])
                if result["disposition"] == "candidate":
                    raise ValueError("revision_cap")
            return self._revise(conn, proposed, old)
        if op not in {"contest", "dismiss", "supersede", "revalidate"}:
            raise ValueError("invalid_operation")
        evidence = self._evidence(operation["evidence_ids"])
        old_scope = normalise_scope(json.loads(old["scope"]))
        if any(
            not in_scope(row, old_scope) or row["pattern_id"] != old["pattern_id"]
            for row in evidence
        ):
            raise ValueError("unrelated_evidence")
        if op == "revalidate":
            scope = json.loads(old["scope"])
            supported = [r for r in evidence if in_scope(r, scope)]
            status = promotion_status(supported, self.config.thresholds.min_sessions, old["kind"])
            status = (
                "active"
                if status == "active" and not json.loads(old["contradicting_evidence"])
                else "contested"
            )
        else:
            status = {"contest": "contested", "dismiss": "dismissed", "supersede": "superseded"}[op]
        result = self._transition(
            conn, old, status, operation["reason"], evidence if op == "revalidate" else None
        )
        if (
            op == "dismiss"
            and operation.get("exclude_evidence")
            and self.actor != "dream"
            and result["disposition"] == "applied"
        ):
            ids = set(json.loads(old["supporting_evidence"]))
            for evidence_id in ids:
                conn.execute("UPDATE observations SET excluded=1 WHERE id=?", (evidence_id,))
            queue_reassessment(conn, ids)
        return result

    def _transition(self, conn, old, status, reason, evidence=None):
        if old["status"] in {"dismissed", "superseded"} and self.actor == "dream":
            raise ValueError("retired_interpretation")
        if self._protected(conn, old) and status != "active" and self.actor == "dream":
            # The revision cap remains visible before protection when both apply.
            protected = True
        else:
            protected = False
        if (
            status in {"dismissed", "superseded"}
            and old["status"] == "active"
            and self.actor == "dream"
        ):
            used = conn.execute(
                "SELECT count(*) FROM knowledge_revisions WHERE run_id=? AND scope=? "
                "AND actor='dream' AND status IN ('dismissed','superseded')",
                (self.run_id, old["scope"]),
            ).fetchone()[0]
            allowed = math.floor(
                self.initial_active.get(old["scope"], 0)
                * self.config.thresholds.max_revision_fraction
            )
            if used >= allowed:
                conn.execute(
                    "UPDATE knowledge_items SET pending_reassessment=1 WHERE id=?", (old["id"],)
                )
                return {"disposition": "candidate", "reason": "revision_cap", "item_id": old["id"]}
        if protected:
            raise ValueError("user_resolution_protected")
        values = {
            k: old[k]
            for k in (
                "kind",
                "scope",
                "summary",
                "confidence",
                "supporting_evidence",
                "contradicting_evidence",
                "latest_support_at",
                "lens_id",
                "pattern_id",
                "user_resolved",
            )
        }
        values.update(status=status, confidence_basis=reason, pending_reassessment=0)
        values["user_resolved"] = int(self._protected(conn, old))
        if evidence is not None:
            values["supporting_evidence"] = canonical(sorted(row["id"] for row in evidence))
            values["latest_support_at"] = latest_support(evidence)
        return self._write(conn, old, values)


def set_learning_disposition(store, item_id, disposition, *, until=None, reason=None):
    if disposition not in {"open", "dismissed", "deferred_until", "addressed"}:
        raise ValueError("invalid_disposition")
    if disposition == "deferred_until" and until is not None and _timestamp(until) is None:
        raise ValueError("invalid_deferral")
    updated = store.connection.execute(
        "UPDATE learning SET disposition=?,deferred_until=?,reason=? WHERE id=?",
        (disposition, until if disposition == "deferred_until" else None, reason, item_id),
    )
    if not updated.rowcount:
        raise ValueError("unknown_learning_recommendation")


def learning_for_use(store, *, now=None) -> list[dict]:
    now = now or datetime.now(UTC)
    return [
        row
        for row in _rows(store.connection.execute("SELECT * FROM learning ORDER BY id"))
        if row["disposition"] == "open"
        or (
            row["disposition"] == "deferred_until"
            and _timestamp(row["deferred_until"]) is not None
            and _timestamp(row["deferred_until"]) <= now
        )
    ]

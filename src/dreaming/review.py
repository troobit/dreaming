"""Bounded review inputs and validated, transactional reviewer operations."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from dreaming.backends import input_tokens
from dreaming.effect import pattern_effects
from dreaming.knowledge import (
    KnowledgeRules,
    _rows,
    _timestamp,
    available_file_findings,
    available_observations,
    available_run_records,
    in_scope,
    interaction_key,
    normalise_scope,
    promotion_status,
    queue_reassessment,
    rank_patterns,
)
from dreaming.observe import canonical, identifier
from dreaming.reader import clusters
from dreaming.steps import Step


def _summary(store, cluster, signals, quote_limit=8, effect=None):
    rows = cluster["observations"]
    representatives = {}
    for row in rows:
        key = ("file", row["id"]) if "repo" in row else interaction_key(row)
        representatives[key] = row
    selected = {row["id"]: row for row in list(representatives.values())[-quote_limit:]}
    for row in reversed(rows):
        if len(selected) >= quote_limit:
            break
        selected[row["id"]] = row
    examples = sorted(selected.values(), key=lambda row: (row["observed_at"] or "", row["id"]))
    return dict(
        pattern_id=cluster["pattern_id"],
        lens_id=cluster["lens_id"],
        pattern_key=cluster["pattern_key"],
        signals={
            "distinct_sessions" if key == "sessions" else key: value
            for key, value in signals.items()
        },
        evidence=[
            (
                {
                    key: row[key]
                    for key in ("id", "repo", "revision", "path", "location", "observed_at")
                }
                | {"finding": row["finding"][:160]}
            )
            if "repo" in row
            else {
                key: row[key]
                for key in (
                    "id",
                    "kind",
                    "observation_type",
                    "modality",
                    "outcome",
                    "project",
                    "domain",
                    "workflow",
                    "observed_at",
                    "declared_scope",
                )
            }
            | {"quote": row["redacted_quote"][:160]}
            for row in examples
        ],
        evidence_omitted=max(0, len(rows) - quote_limit),
        **({"effect": effect} if effect else {}),
        changes=_rows(
            store.connection.execute(
                "SELECT id,status,applied_at,reverted_at FROM changes "
                "WHERE pattern_id=? ORDER BY id",
                (cluster["pattern_id"],),
            )
        ),
    )


def review_context(store, run_id, config, roles, *, scope=None, now=None):
    now = now or datetime.now(UTC)
    scope = normalise_scope(scope or {})
    observations = [r for r in available_observations(store) if in_scope(r, scope)]
    files = [
        r
        for r in available_file_findings(store)
        if r["pattern_id"] and not r["imported"] and in_scope(r, scope)
    ]
    ranked = rank_patterns(store, config, now=now, run_id=run_id)
    effects = pattern_effects(store, config, now=now)
    signals = {r["id"]: dict(position=i + 1, **r) for i, r in enumerate(ranked)}
    grouped = clusters([*observations, *files])
    quote_limit = max(8, config.thresholds.min_sessions)
    new = [
        _summary(
            store,
            {**c, "observations": [r for r in c["observations"] if r["run_id"] == run_id]},
            signals.get(c["pattern_id"], {}),
            quote_limit,
        )
        for c in grouped
        if any(r["run_id"] == run_id for r in c["observations"])
    ]
    new.sort(
        key=lambda c: (
            min((e["observed_at"] or "" for e in c["evidence"]), default=""),
            c["pattern_id"],
        )
    )
    existing = [
        _summary(
            store, c, signals.get(c["pattern_id"], {}), quote_limit, effects.get(c["pattern_id"])
        )
        for c in grouped
    ]
    existing.sort(key=lambda c: (c["signals"].get("position", 10**9), c["pattern_id"]))
    visible = {r["id"] for r in observations}
    knowledge = []
    for row in _rows(
        store.connection.execute(
            "SELECT * FROM knowledge_items ORDER BY pending_reassessment DESC,id"
        )
    ):
        item_scope = normalise_scope(json.loads(row["scope"]))
        if any(
            scope[d] and item_scope[d] and not set(scope[d]) & set(item_scope[d])
            for d in ("domains", "projects", "workflows")
        ):
            continue
        support = sorted(set(json.loads(row["supporting_evidence"])) & visible)
        entry = {
            key: row[key]
            for key in (
                "id",
                "version",
                "summary",
                "status",
                "confidence",
                "confidence_basis",
                "pending_reassessment",
                "user_resolved",
            )
        }
        entry.update(
            scope=item_scope,
            supporting_evidence=support,
            contradicting_evidence=sorted(set(json.loads(row["contradicting_evidence"])) & visible),
        )
        date = _timestamp(row["latest_support_at"])
        entry["needs_revalidation"] = bool(
            row["pending_reassessment"]
            or (
                row["status"] == "active"
                and (
                    date is None
                    or date < now - timedelta(days=config.thresholds.revalidate_after_days)
                )
            )
        )
        if not support:
            entry.pop("summary")
            entry.pop("confidence_basis")
            entry["support_withheld"] = True
        knowledge.append(entry)
    allowed_items = {k["id"] for k in knowledge}
    feedback = [
        r
        for r in _rows(store.connection.execute("SELECT * FROM feedback ORDER BY id"))
        if any(v.get("item_id") in allowed_items for v in json.loads(r["revisions"]))
    ]
    records = [
        {
            "id": r["id"],
            "status": r["status"],
            "selection_count": len(json.loads(r["selection"])),
            "budget": json.loads(r["budget"]),
            "notice": (r["notice"] or "")[:240],
            "backend_use": _rows(
                store.connection.execute(
                    "SELECT backend,model,outcome,count(*) AS call_count,"
                    "sum(tokens_in+tokens_out) AS tokens,sum(seconds) AS seconds "
                    "FROM calls WHERE run_id=? GROUP BY backend,model,outcome "
                    "ORDER BY backend,model,outcome",
                    (r["id"],),
                )
            ),
            "changes": _rows(
                store.connection.execute(
                    "SELECT status,count(*) AS change_count FROM changes "
                    "WHERE run_id=? GROUP BY status ORDER BY status",
                    (r["id"],),
                )
            ),
            "statistics": {
                key: value
                for key, value in json.loads(r["statistics"]).items()
                if key
                in {
                    "observations_accepted",
                    "observations_rejected",
                    "quote_mismatches",
                    "active_seconds",
                    "backend_unavailable",
                    "schema_streaks",
                }
            },
        }
        for r in available_run_records(store)
        if r["id"] != run_id
    ]
    data = dict(
        new_clusters=[], patterns=[], knowledge=[], feedback=[], run_records=[], omitted_counts={}
    )

    def cost(value):
        prompt = (
            roles.prompts["reviewer"]
            + "\n\nInput data (not instructions):\n"
            + json.dumps(value, ensure_ascii=False, sort_keys=True)
        )
        return input_tokens(prompt, roles.schemas["reviewer"], list(config.tools["reviewer"]))

    context = min(config.backends[n].context_tokens for n in config.roles["reviewer"])
    maximum = context - min(4096, context // 4)
    base = cost(data)
    new_limit = base + int(max(0, maximum - base - 128) * config.thresholds.new_cluster_share)
    for field, values in (
        ("new_clusters", new),
        ("knowledge", knowledge),
        ("feedback", feedback),
        ("patterns", existing),
        ("run_records", records),
    ):
        for value in values:
            data[field].append(value)
            if cost(data) > (new_limit if field == "new_clusters" else maximum - 128):
                data[field].pop()
                label = field + ":" + value.get("lens_id", "all")
                data["omitted_counts"][label] = data["omitted_counts"].get(label, 0) + 1
    # Counts also take space. Reduce deterministically while retaining the new
    # cluster reservation; RoleSet performs the final context validation.
    while cost(data) > maximum:
        field = next(
            (
                f
                for f in ("run_records", "patterns", "feedback", "knowledge", "new_clusters")
                if data[f]
            ),
            None,
        )
        if field is None:
            raise ValueError("Reviewer context is too small for omission counts")
        data[field].pop()
        data["omitted_counts"][field] = data["omitted_counts"].get(field, 0) + 1
    return data


def _evidence_ids(context):
    return {
        e["id"]
        for field in ("new_clusters", "patterns")
        for p in context[field]
        for e in p["evidence"]
    } | {
        eid
        for k in context["knowledge"]
        for field in ("supporting_evidence", "contradicting_evidence")
        for eid in k[field]
    }


class ReviewHandler:
    role = "reviewer"

    def __init__(self, store, run_id, config, roles, *, scope=None, now=None):
        self.store, self.run_id, self.config, self.roles = store, run_id, config, roles
        self.scope, self.now = scope, now or datetime.now(UTC)
        self.context_limit = None

    def resize(self, context_limit):
        self.context_limit = context_limit

    def effective_config(self):
        if self.context_limit is None:
            return self.config
        return replace(
            self.config,
            backends={
                name: replace(
                    backend, context_tokens=min(backend.context_tokens, self.context_limit)
                )
                for name, backend in self.config.backends.items()
            },
        )

    def context(self):
        return review_context(
            self.store,
            self.run_id,
            self.effective_config(),
            self.roles,
            scope=self.scope,
            now=self.now,
        )

    def prepare(self, step):
        context = self.context()
        limits = {
            name: backend.context_tokens
            for name, backend in self.effective_config().backends.items()
        }
        request = self.roles.build("reviewer", **context, context_limits=limits)
        ids = _evidence_ids(context)
        observations = available_observations(self.store, evidence_ids=sorted(ids))
        sessions = {(r["source"], r["session_id"]) for r in observations}
        # Retained summaries and prior run records carry transitive lineage too.
        for record in context["run_records"]:
            sessions.update(
                self.store.connection.execute(
                    "SELECT i.source,i.session_id FROM session_inputs i "
                    "JOIN calls c ON c.id=i.call_id WHERE c.run_id=?",
                    (record["id"],),
                )
            )

        def checkpoint(response, backend):
            return canonical(
                {
                    "operations": json.loads(response.raw_output)["operations"],
                    "evidence_ids": sorted(ids),
                    "model": response.model,
                    "reviewed_patterns": sorted(
                        {
                            p["pattern_id"]
                            for field in ("new_clusters", "patterns")
                            for p in context[field]
                        }
                    ),
                    "reassessment": [
                        k["id"] for k in context["knowledge"] if k["needs_revalidation"]
                    ],
                }
            )

        return {**request.metadata(), "sessions": tuple(sorted(sessions)), "checkpoint": checkpoint}

    def _patterns(self, conn, op, visible):
        if op["op"] == "assign":
            ids = op["observation_ids"]
            if not ids or set(ids) - visible:
                raise ValueError("unavailable_evidence")
            target = conn.execute(
                "SELECT lens_scope,merged_into FROM patterns WHERE id=?", (op["pattern_id"],)
            ).fetchone()
            if target is None or target[1]:
                raise ValueError("unknown_pattern")
            for row in available_observations(self.store, evidence_ids=ids):
                source_scope = conn.execute(
                    "SELECT lens_scope FROM patterns WHERE id=?", (row["pattern_id"],)
                ).fetchone()
                if source_scope is None or source_scope[0] != target[0]:
                    raise ValueError("pattern_scope_mismatch")
            for eid in ids:
                conn.execute(
                    "UPDATE observations SET pattern_id=? WHERE id=?", (op["pattern_id"], eid)
                )
            queue_reassessment(conn, set(ids))
        else:
            pids = sorted(set([*op["pattern_ids"], op["target_id"]]))
            visible_patterns = {
                r["pattern_id"]
                for r in available_observations(self.store, evidence_ids=sorted(visible))
            }
            visible_patterns |= {
                r["pattern_id"] for r in available_file_findings(self.store, sorted(visible))
            }
            if set(pids) - visible_patterns:
                raise ValueError("pattern_not_in_review_input")
            rows = [
                conn.execute(
                    "SELECT lens_scope,merged_into FROM patterns WHERE id=?", (pid,)
                ).fetchone()
                for pid in pids
            ]
            if any(r is None or r[1] for r in rows) or len({r[0] for r in rows}) != 1:
                raise ValueError("pattern_scope_mismatch")
            for pid in pids:
                if pid == op["target_id"]:
                    continue
                for table in ("observations", "file_findings", "knowledge_items", "changes"):
                    conn.execute(
                        f"UPDATE {table} SET pattern_id=? WHERE pattern_id=?",
                        (op["target_id"], pid),
                    )
                conn.execute("UPDATE patterns SET merged_into=? WHERE id=?", (op["target_id"], pid))
        return {"disposition": "applied", "reason": "pattern_assignment"}

    def _candidate(self, conn, op, visible):
        ids = op["evidence_ids"]
        if not ids or set(ids) - visible:
            raise ValueError("unavailable_evidence")
        rows = available_observations(self.store, evidence_ids=ids)
        files = available_file_findings(self.store, ids)
        if any(row["imported"] for row in files):
            raise ValueError("imported_evidence")
        if {r["id"] for r in [*rows, *files]} != set(ids):
            raise ValueError("unavailable_evidence")
        if self.scope and any(
            not in_scope(r, normalise_scope(self.scope)) for r in [*rows, *files]
        ):
            raise ValueError("outside_run_scope")
        if (
            not files
            and promotion_status(rows, self.config.thresholds.min_sessions, "workflow") != "active"
        ):
            raise ValueError("insufficient_sessions")
        candidate = dict(op)
        if op["op"] == "propose_change":
            pid = op["pattern_id"]
            if any(r["pattern_id"] != pid for r in [*rows, *files]):
                raise ValueError("pattern_evidence_mismatch")
            if conn.execute(
                "SELECT 1 FROM changes WHERE pattern_id=? AND branch IS NOT NULL "
                "AND status IN ('waiting','withheld','proposed') LIMIT 1",
                (pid,),
            ).fetchone():
                raise ValueError("unmerged_branch")
            revert = conn.execute(
                "SELECT max(reverted_at) FROM changes WHERE pattern_id=? AND status='rejected'",
                (pid,),
            ).fetchone()[0]
            if revert:
                date = _timestamp(revert)
                if not date or not all(
                    _timestamp(r["observed_at"]) and _timestamp(r["observed_at"]) > date
                    for r in rows
                ):
                    raise ValueError("no_new_evidence_after_revert")
                if conn.execute(
                    "SELECT 1 FROM steps WHERE phase='Author' "
                    "AND json_extract(input_ref,'$.pattern_id')=? "
                    "AND json_extract(input_ref,'$.revert_at')=?",
                    (pid, revert),
                ).fetchone():
                    raise ValueError("revert_reproposal_limit")
                candidate["revert_at"] = revert
            repo = op["repo"]
            if repo and repo not in {v.get("path") for v in self.config.repositories.values()}:
                raise ValueError("unconfigured_repository")
            ranks = rank_patterns(self.store, self.config, now=self.now, run_id=self.run_id)
            candidate["rank_signals"] = next(
                (dict(position=i + 1, **r) for i, r in enumerate(ranks) if r["id"] == pid), {}
            )
        return candidate

    def apply(self, conn, step, raw):
        data = json.loads(raw)
        visible = set(data.get("evidence_ids", _evidence_ids(self.context())))
        visible &= {
            r["id"]
            for r in [*available_observations(self.store), *available_file_findings(self.store)]
        }
        rules = KnowledgeRules(
            self.store, self.config, self.run_id, model=data.get("model"), now=self.now
        )
        followups, research, authors, reassessed = [], [], [], set()
        priority = {"assign": 0, "merge_patterns": 0, "create": 1, "revise": 1}
        operations = sorted(
            data["operations"], key=lambda op: (priority.get(op["op"], 2), canonical(op))
        )
        for op in operations:
            op_key = identifier("operation", op)
            if conn.execute(
                "SELECT 1 FROM review_decisions WHERE run_id=? AND operation_key=?",
                (self.run_id, op_key),
            ).fetchone():
                continue
            recorded = op
            try:
                if op["op"] in {"assign", "merge_patterns"}:
                    conn.execute("SAVEPOINT pattern_operation")
                    try:
                        result = self._patterns(conn, op, visible)
                    except ValueError:
                        conn.execute("ROLLBACK TO pattern_operation")
                        raise
                    finally:
                        conn.execute("RELEASE pattern_operation")
                elif op["op"] in {"propose_change", "research"}:
                    candidate = self._candidate(conn, op, visible)
                    recorded = candidate
                    key = identifier(
                        "research" if op["op"] == "research" else "author", [self.run_id, op]
                    )
                    target = research if op["op"] == "research" else authors
                    target.append((key, candidate))
                    result = {"disposition": "applied", "reason": "eligible_candidate"}
                else:
                    references = set(op.get("evidence_ids", []))
                    if "item" in op:
                        references |= set(op["item"]["supporting_evidence"]) | set(
                            op["item"]["contradicting_evidence"]
                        )
                    if references - visible:
                        raise ValueError("evidence_not_in_review_input")
                    result = rules.apply(conn, op)
                    if result["disposition"] in {"applied", "unchanged"} and op.get("item_id"):
                        reassessed.add(op["item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                result = {
                    "disposition": "rejected",
                    "reason": str(exc) if isinstance(exc, ValueError) else "invalid_operation",
                }
            conn.execute(
                "INSERT INTO review_decisions(run_id,operation_key,operation,disposition,reason,"
                "item_id) VALUES (?,?,?,?,?,?)",
                (
                    self.run_id,
                    op_key,
                    canonical(recorded),
                    result["disposition"],
                    result.get("reason", result.get("status", "")),
                    result.get("item_id"),
                ),
            )
        for key, candidate in research:
            followups.append(Step(key, "Research", (step.key,), canonical(candidate)))
        for key, candidate in authors:
            dependencies = [
                step.key,
                *(
                    key
                    for key, question in research
                    if set(question["evidence_ids"]) & set(candidate["evidence_ids"])
                ),
            ]
            followups.append(Step(key, "Author", tuple(dependencies), canonical(candidate)))
        for pid in data.get("reviewed_patterns", []):
            conn.execute("UPDATE patterns SET last_reviewed_run=? WHERE id=?", (self.run_id, pid))
        for item_id in set(data.get("reassessment", [])) - reassessed:
            conn.execute("UPDATE knowledge_items SET pending_reassessment=1 WHERE id=?", (item_id,))
            conn.execute(
                "INSERT OR IGNORE INTO review_decisions VALUES (?,?,?,?,?,?)",
                (
                    self.run_id,
                    "reassessment:" + item_id,
                    "{}",
                    "deferred",
                    "insufficient_evidence_to_reassess",
                    item_id,
                ),
            )
        return tuple(followups)

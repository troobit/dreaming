"""The Review step: clustering, pattern summaries and the reviewer's operations
(4.3, 4.5, 6.5, 9.13, 9.14, 11.3, 11.4, 11.6, 13.8).

A deterministic pass runs first. Observations not yet assigned to a pattern are
grouped by lens and normalised pattern key; a group whose key an existing
pattern already has joins it, and the rest are new clusters. The reviewer sees
new clusters, existing patterns in rank order and earlier dreams' run records,
each as bounded summaries, never transcripts. It returns operations, which the
program applies only when they pass the rules, recording the reason for every
one it refuses. Only the program creates pattern ids.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from dreaming import adopt, budget, config, effect, knowledge, roles, sessions, steps
from dreaming.errors import Rejected
from dreaming.observe import evidence_input_ref, normalise_key, run_request
from dreaming.run import Context, Outcome
from dreaming.steps import NewStep, Step

# Evidence shown per summary: one per session first, newest first.
SUMMARY_EVIDENCE = 10
# Feedback shown per item awaiting reassessment, the latest.
FEEDBACK_SHOWN = 5
# A change on a dream branch that has not been merged (9.14).
UNMERGED = ("proposed", "waiting")
NO_CHANGE = ("failed", "withheld")


# The deterministic pass


@dataclass(frozen=True)
class Cluster:
    """New observations sharing a lens and a normalised pattern key."""

    lens: str
    key: str
    rows: tuple

    @property
    def ref(self) -> str:
        """How the reviewer names the cluster."""
        return f"{self.lens}/{self.key}"


def _by_date(rows: Iterable[Mapping]) -> list:
    return sorted(rows, key=lambda row: (row["observed_on"] or "", row["evidence_id"]))


def cluster(rows: Iterable[Mapping]) -> list[Cluster]:
    """Group observations by lens and normalised key, in a fixed order, so the
    order they are read in never changes the clusters."""
    groups: dict[tuple[str, str], list] = {}
    for row in rows:
        key = normalise_key(row["pattern_key"] or "", row["lens"])
        if key:
            groups.setdefault((row["lens"], key), []).append(row)
    return [
        Cluster(lens, key, tuple(_by_date(members)))
        for (lens, key), members in sorted(groups.items())
    ]


def signals(rows: Sequence[Mapping]) -> knowledge.Signals:
    return knowledge.Signals(
        observations=len(rows),
        sessions=len({row["interaction"] for row in rows}),
        projects=len({row["project"] for row in rows if row["project"]}),
        last_date=max(
            (row["observed_on"] for row in rows if row["observed_on"]), default=None
        ),
    )


def summary_evidence(rows: Sequence[Mapping], quote_chars: int) -> tuple:
    """Up to `SUMMARY_EVIDENCE` observations, one per session first and the
    newest first, shown in date order."""
    newest = list(reversed(_by_date(rows)))
    picked, sessions_seen = [], set()
    for row in newest:
        if row["interaction"] not in sessions_seen:
            picked.append(row)
            sessions_seen.add(row["interaction"])
    picked += [row for row in newest if row not in picked]
    return tuple(
        roles.Evidence.from_row(row, quote_chars)
        for row in _by_date(picked[:SUMMARY_EVIDENCE])
    )


# What the reviewer sees


def _feedback(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Feedback from consuming tools on each item's revisions, oldest first
    (14.5)."""
    found: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT * FROM feedback ORDER BY received_at, id"):
        for cited in json.loads(row["revisions"]):
            found.setdefault(cited["item_id"], []).append(
                {
                    "id": row["id"],
                    "version": cited["version"],
                    "use": cited["use"],
                    "outcome": row["outcome"],
                    "basis": row["basis"],
                    "check": row["check_name"],
                    "received_at": row["received_at"],
                }
            )
    return found


def _items(conn: sqlite3.Connection, cfg: dict, today: date) -> dict[str, list[dict]]:
    """Items resting on each pattern, as the reviewer sees them; superseded items
    are left out. `revalidate` marks an active item whose latest support is older
    than `revalidate_after_days`, and an item pending reassessment shows its
    latest feedback (11.7)."""
    limit = cfg["thresholds"]["revalidate_after_days"]
    feedback = _feedback(conn)
    found: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT r.*, k.pattern_id, k.pending_reassessment FROM knowledge_items k"
        " JOIN knowledge_revisions r ON r.item_id = k.id AND r.version = k.version"
        " WHERE k.status != 'superseded' AND k.pattern_id IS NOT NULL ORDER BY k.id"
    )
    for row in rows:
        latest = row["latest_support_date"]
        stale = not latest or (today - date.fromisoformat(latest[:10])).days > limit
        found.setdefault(row["pattern_id"], []).append(
            {
                "id": row["item_id"],
                "version": row["version"],
                "kind": row["kind"],
                "status": row["status"],
                "content": row["content"],
                "scope": json.loads(row["scope"]),
                "confidence": row["confidence"],
                "pending_reassessment": bool(row["pending_reassessment"]),
                "revalidate": row["status"] == "active" and stale,
                "feedback": (
                    feedback.get(row["item_id"], [])[-FEEDBACK_SHOWN:]
                    if row["pending_reassessment"]
                    else []
                ),
            }
        )
    return found


def _changes(conn: sqlite3.Connection, cfg: dict) -> dict[str, list[dict]]:
    """Each pattern's changes; an applied one carries its effect, before and
    after it was applied, as association (11.1)."""
    found: dict[str, list[dict]] = {}
    measure = effect.Measure(conn, cfg)
    rows = conn.execute(
        "SELECT * FROM changes WHERE pattern_id IS NOT NULL ORDER BY created_at, id"
    )
    for row in rows:
        found.setdefault(row["pattern_id"], []).append(
            {
                "id": row["id"],
                "run": row["run"],
                "repo": row["repo"],
                "status": row["status"],
                "applied_at": row["applied_at"],
                "reverted_at": row["reverted_at"],
                "effect": measure.change(row).to_dict() if row["applied_at"] else None,
            }
        )
    return found


def pattern_status(changes: Sequence[Mapping], run_id: str) -> str:
    """`recurring` when the latest applied change's pattern recurs at its
    earlier rate, so it may be a candidate again (11.2)."""
    if any(c["status"] in UNMERGED and c["run"] != run_id for c in changes):
        return "unmerged_change"
    applied = [c for c in changes if c["status"] == "applied" and not c["reverted_at"]]
    if applied:
        latest = max(applied, key=lambda c: (c["applied_at"] or "", c["id"]))
        recurring = (latest["effect"] or {}).get("recurring")
        return "recurring" if recurring else "changed"
    if changes and changes[-1]["status"] == "rejected":
        return "change_rejected"
    return "open"


def unreviewed_dreams(conn: sqlite3.Connection, pattern: Mapping, run_id: str) -> int:
    """Dreams since the pattern was last reviewed, or created."""
    since = pattern["last_reviewed_run"] or pattern["created_run"]
    return conn.execute(
        "SELECT COUNT(*) FROM runs WHERE kind = 'dream' AND status != 'discarded'"
        " AND imported = 0 AND id != :run AND started_at > COALESCE("
        "(SELECT started_at FROM runs WHERE id = :since), '')",
        {"run": run_id, "since": since},
    ).fetchone()[0]


def run_records(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Earlier dreams, newest first, as counts: evidence about dreaming itself
    (7.5), holding no session's text or identifiers."""
    records = []
    rows = conn.execute(
        "SELECT * FROM runs WHERE id != ? AND status != 'discarded' AND imported = 0"
        " ORDER BY started_at DESC, id DESC",
        (run_id,),
    )
    for row in rows:
        run = row["id"]
        step_counts: dict[str, dict[str, int]] = {}
        for phase, state, count in conn.execute(
            "SELECT phase, state, COUNT(*) FROM steps WHERE run = ?"
            " GROUP BY phase, state ORDER BY phase, state",
            (run,),
        ):
            step_counts.setdefault(phase, {})[state] = count

        def grouped(sql: str, run: str = run) -> dict[str, int]:
            return {key: count for key, count in conn.execute(sql, (run,))}

        records.append(
            {
                "run": run,
                "kind": row["kind"],
                "status": row["status"],
                "requester": row["requester"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "error": row["error"],
                "tokens": budget.tokens_used(conn, run),
                "steps": step_counts,
                "observations": conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE run = ?", (run,)
                ).fetchone()[0],
                "rejected": grouped(
                    "SELECT what, COUNT(*) FROM rejections WHERE run = ?"
                    " GROUP BY what ORDER BY what"
                ),
                "candidates": grouped(
                    "SELECT proposal, COUNT(*) FROM candidates WHERE run = ?"
                    " GROUP BY proposal ORDER BY proposal"
                ),
                "changes": grouped(
                    "SELECT status, COUNT(*) FROM changes WHERE run = ?"
                    " GROUP BY status ORDER BY status"
                ),
                "outcomes": effect.stage_outcomes(conn, run),
            }
        )
    return records


@dataclass
class Reviewed:
    """What `prepare` found and the reviewer answered."""

    cfg: dict
    now: str
    today: date
    automatic: dict[str, str]  # evidence id -> the pattern its key already has
    clusters: dict[str, Cluster]  # the new clusters shown, by ref
    shown: list[str]  # the existing patterns shown
    output: dict | None = None
    backend: str | None = None
    model: str | None = None
    reason: str | None = None
    lenses: dict[str, config.Lens] = field(default_factory=dict)


class ReviewPhase:
    def __init__(self, instructions: roles.InstructionSet) -> None:
        self.instructions = instructions

    def prepare(self, ctx: Context, step: Step) -> Reviewed:
        conn, cfg, run_id = ctx.conn, ctx.cfg, ctx.run_id
        # Changes the user has merged, deleted or reverted since an earlier dream
        # made them are recorded before the reviewer sees each pattern's changes
        # (9.13, 9.14).
        adopt.detect_merges(conn, cfg, run_id, ctx.runner.now())
        adopt.detect_reverts(conn, cfg)
        today = ctx.clock.now().date()
        scope = run_request(conn, run_id).scope
        lenses = {lens.id: lens for lens in self.instructions.lenses}
        allowed = None
        if scope is not None:
            in_scope = sessions.scope_lenses(self.instructions.lenses, scope, cfg)
            allowed = {lens.id for lens in in_scope}

        def wanted(lens: str) -> bool:
            return allowed is None or lens in allowed

        rows = [r for r in knowledge.counted_observations(conn) if wanted(r["lens"])]
        patterns = {
            row["id"]: row
            for row in conn.execute(
                "SELECT * FROM patterns WHERE merged_into IS NULL ORDER BY id"
            )
            if wanted(row["lens"])
        }
        by_key: dict[tuple[str, str], str] = {}
        for pattern in patterns.values():
            by_key.setdefault((pattern["lens"], pattern["key"]), pattern["id"])

        automatic: dict[str, str] = {}
        members: dict[str, list] = {pid: [] for pid in patterns}
        unassigned = []
        for row in rows:
            pid = row["pattern_id"]
            if pid is None:
                key = normalise_key(row["pattern_key"] or "", row["lens"])
                pid = by_key.get((row["lens"], key))
                if pid is None:
                    unassigned.append(row)
                    continue
                automatic[row["evidence_id"]] = pid
            if pid in members:
                members[pid].append(row)
        clusters = cluster(unassigned)

        items = _items(conn, cfg, today)
        changes = _changes(conn, cfg)
        quote_chars = cfg["thresholds"]["quote_chars"]
        renewed = set(automatic.values())
        considered = [pid for pid in patterns if members[pid] or items.get(pid)]
        found = {pid: signals(members[pid]) for pid in considered}
        scores = dict(knowledge.rank_patterns(found, cfg, today))

        def attention(pid: str) -> bool:
            return pid in renewed or any(
                item["pending_reassessment"] or item["revalidate"]
                for item in items.get(pid, ())
            )

        boost = cfg["thresholds"]["boost_after_dreams"]
        due = {
            pid: attention(pid)
            or unreviewed_dreams(conn, patterns[pid], run_id) >= boost
            for pid in considered
        }
        order = sorted(considered, key=lambda pid: (not due[pid], -scores[pid], pid))
        existing = [
            roles.PatternSummary(
                key=patterns[pid]["key"],
                lens=patterns[pid]["lens"],
                pattern_id=pid,
                observations=found[pid].observations,
                sessions=found[pid].sessions,
                projects=found[pid].projects,
                last_date=found[pid].last_date,
                status=pattern_status(changes.get(pid, []), run_id),
                evidence=summary_evidence(members[pid], quote_chars),
                knowledge=tuple(items.get(pid, ())),
                changes=tuple(changes.get(pid, ())),
            )
            for pid in order
        ]
        new = [
            roles.PatternSummary(
                key=c.ref,
                lens=c.lens,
                pattern_id=None,
                **_counts(signals(c.rows)),
                status="new",
                evidence=summary_evidence(c.rows, quote_chars),
            )
            for c in clusters
        ]
        reviewed = Reviewed(
            cfg, ctx.runner.now(), today, automatic, {}, [], lenses=lenses
        )
        if not clusters and not any(attention(pid) for pid in considered):
            reviewed.reason = "nothing new to review"
            return reviewed

        reviewer = self.instructions.roles["reviewer"]
        data = roles.reviewer_input(
            cfg,
            reviewer,
            new=new,
            existing=existing,
            records=run_records(conn, run_id),
        )
        reviewed.clusters = {c.ref: c for c in clusters[: len(data["new_clusters"])]}
        reviewed.shown = order[: len(data["patterns"])]
        reviewed.output = ctx.call(
            "reviewer",
            reviewer.prompt(data),
            reviewer.schema,
            input_ref=_input_ref(conn, data),
        )
        reviewed.backend, reviewed.model = steps.answered_by(conn, run_id, step.key)
        return reviewed

    def apply(self, conn: sqlite3.Connection, step: Step, reviewed: Reviewed):
        conn.executemany(
            "UPDATE observations SET pattern_id = ?"
            " WHERE evidence_id = ? AND pattern_id IS NULL",
            [(pid, evidence) for evidence, pid in reviewed.automatic.items()],
        )
        if reviewed.output is None:
            return Outcome(reason=reviewed.reason)
        applier = _Applier(conn, step, reviewed)
        follow_up = applier.run(reviewed.output)
        return Outcome(follow_up=follow_up)


def _counts(s: knowledge.Signals) -> dict:
    return {
        "observations": s.observations,
        "sessions": s.sessions,
        "projects": s.projects,
        "last_date": s.last_date,
    }


def _input_ref(conn: sqlite3.Connection, data: Mapping) -> str:
    """The sessions whose observations the reviewer's input holds (2.11)."""
    shown = [
        evidence["id"]
        for group in ("new_clusters", "patterns")
        for summary in data[group]
        for evidence in summary["evidence"]
    ]
    return evidence_input_ref(conn, shown)


class _Applier:
    """Applies one reviewer answer inside the Review step's transaction."""

    def __init__(self, conn: sqlite3.Connection, step: Step, reviewed: Reviewed):
        self.conn = conn
        self.step = step
        self.run_id = step.run
        self.r = reviewed
        self.cfg = reviewed.cfg
        self.assigned: dict[str, str] = {}  # cluster ref -> pattern id
        self.merged: dict[str, str] = {}  # merged pattern id -> the one kept
        self.created: list[str] = []

    def reject(self, what: str, subject: str | None, reason: str, detail: Mapping):
        steps.record_rejection(
            self.conn, self.run_id, self.step.key, what, subject, reason, dict(detail)
        )

    def run(self, output: Mapping) -> list[NewStep]:
        self.assign(output["assignments"])
        for merge in output["pattern_merges"]:
            try:
                self.merge_patterns(merge)
            except Rejected as rejected:
                self.reject("pattern_merge", merge["into"], rejected.reason, merge)
        reviser = knowledge.Reviser(
            self.conn,
            self.cfg,
            now=self.r.now,
            run_id=self.run_id,
            backend=self.r.backend,
            model=self.r.model,
        )
        for index, op in enumerate(output["knowledge"]):
            try:
                reviser.apply({**op, "pattern_id": self.item_pattern(op)}, index)
            except Rejected as rejected:
                subject = op["item_id"] or op["content"][:80]
                self.reject("knowledge", subject, rejected.reason, op)
        for merge in output["item_merges"]:
            try:
                reviser.merge(merge["item_ids"], merge["into"], merge["reason"])
            except Rejected as rejected:
                self.reject("item_merge", merge["into"], rejected.reason, merge)
        accepted = self.candidates(output["candidates"])
        follow_up = self.questions(output["questions"], accepted)
        reviewed = {self.resolve(p) for p in [*self.r.shown, *self.assigned.values()]}
        self.conn.executemany(
            "UPDATE patterns SET last_reviewed_run = ? WHERE id = ?",
            [(self.run_id, pid) for pid in sorted(reviewed - {None})],
        )
        return follow_up

    # Patterns (6.5, 11.4)

    def live(self, pattern_id: str | None) -> Mapping | None:
        if not pattern_id:
            return None
        return self.conn.execute(
            "SELECT * FROM patterns WHERE id = ? AND merged_into IS NULL", (pattern_id,)
        ).fetchone()

    def resolve(self, ref: str | None) -> str | None:
        """The live pattern a reviewer's reference names: a shown cluster's
        pattern, or an existing pattern after any merge."""
        if not ref:
            return None
        if ref in self.assigned:
            return self.assigned[ref]
        ref = self.merged.get(ref, ref)
        return ref if self.live(ref) is not None else None

    def new_pattern(self, found: Cluster) -> str:
        pattern_id = knowledge.new_id("p", self.run_id, found.lens, found.key)
        lens = self.r.lenses.get(found.lens)
        scope = {}
        if lens is not None:
            scope = {
                "domains": list(lens.domains),
                "projects": list(lens.projects),
                "workflows": list(lens.workflows),
            }
        self.conn.execute(
            "INSERT INTO patterns (id, key, lens, scope, created_at, created_run)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                pattern_id,
                found.key,
                found.lens,
                json.dumps(scope),
                self.r.now,
                self.run_id,
            ),
        )
        self.created.append(pattern_id)
        return pattern_id

    def assign(self, assignments: Sequence[Mapping]) -> None:
        """Every shown cluster joins the pattern the reviewer named, or a new one
        the program creates; clusters not shown stay for a later review."""
        for assignment in assignments:
            ref, target = assignment["cluster"], assignment["pattern_id"]
            if ref not in self.r.clusters:
                reason = "no such new cluster was shown"
            elif ref in self.assigned:
                reason = "the cluster was already assigned"
            elif target is not None and self.live(target) is None:
                reason = f"no pattern {target}"
            else:
                found = self.r.clusters[ref]
                self.assigned[ref] = target or self.new_pattern(found)
                continue
            self.reject("assignment", ref, reason, assignment)
        for ref, found in self.r.clusters.items():
            if ref not in self.assigned:
                self.assigned[ref] = self.new_pattern(found)
        for ref, pattern_id in self.assigned.items():
            self.conn.executemany(
                "UPDATE observations SET pattern_id = ?"
                " WHERE evidence_id = ? AND pattern_id IS NULL",
                [(pattern_id, row["evidence_id"]) for row in self.r.clusters[ref].rows],
            )

    def merge_patterns(self, merge: Mapping) -> None:
        ids = list(dict.fromkeys(merge["pattern_ids"]))
        into = merge["into"]
        if into not in ids or len(ids) < 2:
            raise Rejected(
                "a merge names at least two patterns, including the one kept"
            )
        missing = [pid for pid in ids if self.live(pid) is None]
        if missing:
            raise Rejected(f"no pattern {', '.join(missing)}")
        for other in ids:
            if other == into:
                continue
            for table in (
                "observations",
                "file_findings",
                "knowledge_items",
                "changes",
                "candidates",
                "learning",
            ):
                self.conn.execute(
                    f"UPDATE {table} SET pattern_id = ? WHERE pattern_id = ?",
                    (into, other),
                )
            self.conn.execute(
                "UPDATE patterns SET merged_into = ? WHERE id = ?", (into, other)
            )
            self.merged[other] = into
            for ref, pid in self.assigned.items():
                if pid == other:
                    self.assigned[ref] = into

    # Evidence

    def check_subject(self, pattern_id: str, evidence: Iterable[str]) -> None:
        """Observations of earlier dreams are evidence about dreaming only (1.5)."""
        pattern = self.live(pattern_id)
        if pattern is None or pattern["lens"] == roles.DREAM_LENS:
            return
        row = self.conn.execute(
            "SELECT evidence_id FROM observations WHERE subject = 'dreaming'"
            " AND evidence_id IN (SELECT value FROM json_each(?)) LIMIT 1",
            (json.dumps(list(evidence)),),
        ).fetchone()
        if row is not None:
            raise Rejected(
                f"cites {row[0]}, an observation of an earlier dream, as evidence "
                "about the user's work (1.5)"
            )

    def item_pattern(self, op: Mapping) -> str | None:
        """The pattern an item rests on: the one named, or for a new item the one
        most of its supporting evidence belongs to."""
        pattern_id = self.resolve(op["pattern"])
        if op["pattern"] and pattern_id is None:
            raise Rejected(f"no pattern or shown cluster {op['pattern']}")
        if pattern_id is None and op["op"] == "create":
            rows = self.conn.execute(
                "SELECT pattern_id FROM observations WHERE pattern_id IS NOT NULL"
                " AND evidence_id IN (SELECT value FROM json_each(?))",
                (json.dumps(op["supporting"]),),
            )
            counts = Counter(self.resolve(row[0]) for row in rows)
            counts.pop(None, None)
            if not counts:
                raise Rejected("the item rests on no pattern")
            pattern_id = min(counts, key=lambda pid: (-counts[pid], pid))
        if pattern_id is not None:
            self.check_subject(pattern_id, [*op["supporting"], *op["contradicting"]])
        return pattern_id

    # Candidates (4.5, 9.13, 9.14, 11.2, 11.3, 15.1)

    def candidates(self, proposed: Sequence[Mapping]) -> dict[int, str]:
        found = knowledge.pattern_signals(self.conn)
        ranked = knowledge.rank_patterns(found, self.cfg, self.r.today)
        positions = {pid: number for number, (pid, _) in enumerate(ranked, start=1)}
        accepted: dict[int, str] = {}
        for index, candidate in enumerate(proposed):
            try:
                accepted[index] = self.candidate(index, candidate, found, positions)
            except Rejected as rejected:
                self.reject(
                    "candidate", candidate["pattern"], rejected.reason, candidate
                )
        return accepted

    def candidate(
        self,
        index: int,
        candidate: Mapping,
        found: Mapping[str, knowledge.Signals],
        positions: Mapping[str, int],
    ) -> str:
        pattern_id = self.resolve(candidate["pattern"])
        if pattern_id is None:
            raise Rejected(f"no pattern or shown cluster {candidate['pattern']}")
        pattern = self.live(pattern_id)
        lens = self.r.lenses.get(pattern["lens"])
        if lens is None:
            raise Rejected(f"lens {pattern['lens']} is not in the instruction set")
        proposal = candidate["proposal"]
        if proposal not in lens.may_propose:
            raise Rejected(f"lens {lens.id} may not propose a {proposal}")
        evidence = candidate["evidence"]
        if not evidence:
            raise Rejected("a candidate needs evidence")
        knowledge.check_evidence(self.conn, evidence)
        self.check_subject(pattern_id, evidence)
        kind = "learning" if proposal == "learning" else ""
        support = knowledge.item_support(self.conn, kind, evidence)
        if not knowledge.support_holds(support, self.cfg):
            raise Rejected(
                f"its evidence spans {support.sessions} of the "
                f"{self.cfg['thresholds']['min_sessions']} distinct sessions "
                "needed (4.5)"
            )
        earlier = self.conn.execute(
            "SELECT id FROM candidates WHERE run = ? AND pattern_id = ?",
            (self.run_id, pattern_id),
        ).fetchone()
        if earlier is not None:
            raise Rejected(f"the pattern already has candidate {earlier[0]}")
        if proposal == "change":
            self.check_change(pattern_id, candidate["repo"], evidence)
        else:
            row = self.conn.execute(
                "SELECT id, disposition FROM learning WHERE pattern_id = ?",
                (pattern_id,),
            ).fetchone()
            if row is not None:
                raise Rejected(
                    f"the pattern already has learning recommendation {row['id']} "
                    f"({row['disposition']}) (15.3)"
                )
        s = found.get(pattern_id, knowledge.Signals(0, 0, 0, None))
        candidate_id = knowledge.new_id("c", self.run_id, index, pattern_id)
        self.conn.execute(
            "INSERT INTO candidates (id, run, pattern_id, proposal, repo, summary,"
            " evidence, position, signals, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                self.run_id,
                pattern_id,
                proposal,
                candidate["repo"] if proposal == "change" else None,
                candidate["summary"],
                json.dumps(sorted(set(evidence))),
                positions.get(pattern_id),
                json.dumps(
                    {
                        "sessions": s.sessions,
                        "projects": s.projects,
                        "observations": s.observations,
                        "last_date": s.last_date,
                        "recency": round(
                            knowledge.recency(s, self.cfg, self.r.today), 4
                        ),
                        "score": round(knowledge.score(s, self.cfg, self.r.today), 4),
                    }
                ),
                candidate["reason"],
                self.r.now,
            ),
        )
        return candidate_id

    def check_change(self, pattern_id: str, repo: str | None, evidence: Sequence[str]):
        if repo not in self.cfg["repositories"]:
            raise Rejected(f"{repo} is not a configured repository")
        changes = self.conn.execute(
            "SELECT * FROM changes WHERE pattern_id = ? ORDER BY created_at, id",
            (pattern_id,),
        ).fetchall()
        if any(c["status"] in UNMERGED and c["run"] != self.run_id for c in changes):
            raise Rejected(
                "an earlier dream's change for this pattern is still unmerged on its "
                "branch (9.14)"
            )
        changed = any(
            c["status"] == "applied" and not c["reverted_at"] for c in changes
        )
        if changed and effect.recurs(self.conn, self.cfg, pattern_id) is None:
            raise Rejected(
                "the pattern was changed already; it becomes a candidate again only "
                "by recurring at its pre-change rate after "
                f"{self.cfg['thresholds']['recurrence_sessions']} later sessions (11.2)"
            )
        made = [c for c in changes if c["status"] not in NO_CHANGE]
        most = self.cfg["thresholds"]["max_changes_per_pattern"]
        if len(made) >= most:
            raise Rejected(f"the pattern has had {len(made)} changes, the most allowed")
        rejected = [
            c for c in changes if c["status"] == "rejected" and c["reverted_at"]
        ]
        if not rejected:
            return
        reverted = max(c["reverted_at"] for c in rejected)
        again = self.conn.execute(
            "SELECT id FROM candidates WHERE pattern_id = ? AND proposal = 'change'"
            " AND created_at > ?",
            (pattern_id, reverted),
        ).fetchone()
        if again is not None:
            raise Rejected(
                f"proposed again once already ({again[0]}) since the user reverted "
                "its change (9.13)"
            )
        newest = self.conn.execute(
            "SELECT MAX(observed_on) FROM observations"
            " WHERE evidence_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(evidence)),),
        ).fetchone()[0]
        if not newest or newest[:10] <= reverted[:10]:
            raise Rejected(
                "the user reverted this pattern's change; proposing it again needs "
                "observations newer than the revert (9.13)"
            )

    # Research questions and follow-up steps

    def questions(
        self, questions: Sequence[Mapping], accepted: Mapping[int, str]
    ) -> list[NewStep]:
        research: dict[str, list[str]] = {}
        planned: list[NewStep] = []
        for question in questions:
            text, index = question["question"].strip(), question["candidate"]
            if not text:
                self.reject("question", None, "the question is empty", question)
                continue
            if index is not None and index not in accepted:
                self.reject(
                    "question",
                    text[:80],
                    f"candidate {index} was not accepted",
                    question,
                )
                continue
            candidate_id = accepted.get(index) if index is not None else None
            question_id = knowledge.new_id("q", self.run_id, candidate_id, text)
            self.conn.execute(
                "INSERT OR IGNORE INTO questions (id, run, candidate_id, question,"
                " reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    question_id,
                    self.run_id,
                    candidate_id,
                    text,
                    question["reason"],
                    self.r.now,
                ),
            )
            key = f"research:{question_id}"
            if all(step.key != key for step in planned):
                planned.append(NewStep(key, "research", input_ref=question_id))
            if candidate_id is not None:
                research.setdefault(candidate_id, []).append(key)
        authors = []
        for candidate_id in accepted.values():
            keys = tuple(dict.fromkeys(research.get(candidate_id, ())))
            authors.append(f"author:{candidate_id}")
            planned.append(
                NewStep(authors[-1], "author", depends_on=keys, input_ref=candidate_id)
            )
        if authors:
            # Adoption plans its linked groups once every change is authored.
            planned.append(NewStep(adopt.PLAN_KEY, "adopt", depends_on=tuple(authors)))
        return planned

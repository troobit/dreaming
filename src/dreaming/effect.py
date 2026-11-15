"""Effect of applied changes, re-candidacy, calibration statistics and outcomes
by stage (6.7, 7.5, 11.1, 11.2, 11.6; design, Risks and Assumptions).

A change's effect is measured on its pattern: the pattern's counted observations
and the sessions read under the pattern's lens, before and after the change was
applied (for a reverted change, the after period ends at the revert). Each
observation is placed by its session's start, as the sessions are, so both
counts use the same split. A session counts once per interaction, and forgotten
sessions and excluded evidence never count (2.11, 13.6). The numbers are raw
and labelled as association, not proof that the change caused a difference.

The evidence is insufficient when the change cites file findings only, when no
session of the lens scope was read before the change or the pattern was not
observed then (a zero baseline), or when either period has fewer than
`min_sessions_for_rate` sessions. A pattern whose latest applied change has at
least `recurrence_sessions` later sessions and recurs at or above its
pre-change rate is a candidate again (11.2); the reviewer sees the change and
its effect as context.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from dreaming import knowledge, sessions

LABEL = "association, not proof"


@dataclass(frozen=True)
class Effect:
    change_id: str
    pattern_id: str
    summary: str
    applied_at: str
    reverted_at: str | None
    before_observations: int
    before_sessions: int
    after_observations: int
    after_sessions: int
    insufficient: str | None
    recurring: bool

    @property
    def before_rate(self) -> float | None:
        return _rate(self.before_observations, self.before_sessions)

    @property
    def after_rate(self) -> float | None:
        return _rate(self.after_observations, self.after_sessions)

    def to_dict(self) -> dict:
        """As the reviewer sees an earlier change's effect."""
        return {
            "before": {
                "observations": self.before_observations,
                "sessions": self.before_sessions,
            },
            "after": {
                "observations": self.after_observations,
                "sessions": self.after_sessions,
            },
            "insufficient": self.insufficient,
            "recurring": self.recurring,
            "label": LABEL,
        }


def _rate(observations: int, count: int) -> float | None:
    return observations / count if count else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _time(*values: str | None) -> datetime | None:
    for value in values:
        parsed = sessions.parse_time(value)
        if parsed is not None:
            return parsed
    return None


class Measure:
    """Measures changes against the store as it is now. Sessions in each lens
    scope are read once per instance."""

    def __init__(self, conn: sqlite3.Connection, cfg: dict) -> None:
        self.conn = conn
        self.cfg = cfg
        self.scopes: dict[str, list[datetime]] = {}

    def scope(self, lens: str) -> list[datetime]:
        """When each interaction read under `lens` started."""
        if lens not in self.scopes:
            started: dict[str, datetime] = {}
            rows = self.conn.execute(
                "SELECT COALESCE(thread_id, source || ':' || session_id)"
                " AS interaction, started_at FROM sessions_seen"
                " WHERE state = 'read' AND forgotten = 0"
                " AND EXISTS (SELECT 1 FROM json_each(lens_versions) WHERE key = ?)",
                (lens,),
            )
            for row in rows:
                when = _time(row["started_at"])
                if when is not None:
                    earliest = started.get(row["interaction"], when)
                    started[row["interaction"]] = min(earliest, when)
            self.scopes[lens] = list(started.values())
        return self.scopes[lens]

    def observed(self, pattern_id: str) -> list[datetime]:
        """When the session of each counted observation of the pattern started."""
        rows = self.conn.execute(
            "SELECT c.observed_on, s.started_at"
            f" FROM ({knowledge.COUNTED_OBSERVATIONS}) c LEFT JOIN sessions_seen s"
            " ON s.source = c.source AND s.session_id = c.session_id"
            " WHERE c.pattern_id = ?",
            (pattern_id,),
        )
        found = [_time(row["started_at"], row["observed_on"]) for row in rows]
        return [when for when in found if when is not None]

    def change(self, change: Mapping) -> Effect:
        applied = _time(change["applied_at"])
        ended = _time(change["reverted_at"])
        pattern = self.conn.execute(
            "SELECT lens FROM patterns WHERE id = ?", (change["pattern_id"],)
        ).fetchone()
        scope = self.scope(pattern["lens"]) if pattern else []
        observed = self.observed(change["pattern_id"])

        def split(times: list[datetime]) -> tuple[int, int]:
            before = sum(1 for t in times if t < applied)
            after = sum(
                1 for t in times if t >= applied and (ended is None or t < ended)
            )
            return before, after

        before_sessions, after_sessions = split(scope)
        before_observations, after_observations = split(observed)
        insufficient = self._insufficient(
            change, before_sessions, after_sessions, before_observations
        )
        recurring = (
            insufficient is None
            and after_sessions >= max(self.cfg["thresholds"]["recurrence_sessions"], 1)
            and after_observations / after_sessions
            >= before_observations / before_sessions
        )
        return Effect(
            change_id=change["id"],
            pattern_id=change["pattern_id"],
            summary=change["summary"] or change["id"],
            applied_at=change["applied_at"],
            reverted_at=change["reverted_at"],
            before_observations=before_observations,
            before_sessions=before_sessions,
            after_observations=after_observations,
            after_sessions=after_sessions,
            insufficient=insufficient,
            recurring=recurring,
        )

    def _insufficient(
        self, change: Mapping, before: int, after: int, observed_before: int
    ) -> str | None:
        cited = self.conn.execute(
            "SELECT 1 FROM observations"
            " WHERE evidence_id IN (SELECT value FROM json_each(?)) LIMIT 1",
            (change["evidence"],),
        ).fetchone()
        needed = self.cfg["thresholds"]["min_sessions_for_rate"]
        if cited is None:
            return "the change cites file findings only"
        if before == 0:
            return "no session in its lens scope was read before the change"
        if before < needed:
            return (
                f"{_plural(before, 'session')} before the change, fewer than the "
                f"{needed} needed"
            )
        if after < max(needed, 1):
            return (
                f"{_plural(after, 'session')} after the change, fewer than the "
                f"{needed} needed"
            )
        if observed_before == 0:
            return "the pattern was not observed before the change"
        return None


def measured(conn: sqlite3.Connection, cfg: dict) -> list[Effect]:
    """Every applied change's effect, oldest first (11.1)."""
    measure = Measure(conn, cfg)
    rows = conn.execute(
        "SELECT * FROM changes WHERE pattern_id IS NOT NULL AND applied_at IS NOT NULL"
        " ORDER BY applied_at, id"
    )
    return [measure.change(row) for row in rows]


def recurs(conn: sqlite3.Connection, cfg: dict, pattern_id: str) -> Effect | None:
    """The effect of the pattern's latest applied change when the pattern recurs
    at or above its pre-change rate after enough later sessions (11.2)."""
    row = conn.execute(
        "SELECT * FROM changes WHERE pattern_id = ? AND status = 'applied'"
        " AND reverted_at IS NULL AND applied_at IS NOT NULL"
        " ORDER BY applied_at DESC, id DESC LIMIT 1",
        (pattern_id,),
    ).fetchone()
    if row is None:
        return None
    found = Measure(conn, cfg).change(row)
    return found if found.recurring else None


# Calibration (design, Risks and Assumptions)


def session_distribution(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """How many patterns have each number of distinct sessions, for choosing
    `min_sessions` and `min_sessions_for_rate` from what dreams observe."""
    counts = Counter(s.sessions for s in knowledge.pattern_signals(conn).values())
    return sorted(counts.items())


def replay_against_recurrence(conn: sqlite3.Connection, cfg: dict) -> list[dict]:
    """For each applied change that was replayed (9.16), the replay's counts
    beside whether its pattern recurred at its earlier rate after the change."""
    measure = Measure(conn, cfg)
    rows = conn.execute(
        "SELECT * FROM changes WHERE pattern_id IS NOT NULL AND applied_at IS NOT NULL"
        " AND id IN (SELECT change_id FROM replays) ORDER BY applied_at, id"
    )
    found = []
    for row in rows:
        replays = conn.execute(
            "SELECT judgement FROM replays WHERE change_id = ? AND recurs_without = 1",
            (row["id"],),
        ).fetchall()
        result = measure.change(row)
        if result.insufficient:
            later = f"insufficient: {result.insufficient}"
        elif result.after_rate >= result.before_rate:
            later = "recurs"
        else:
            later = "reduced"
        found.append(
            {
                "change_id": row["id"],
                "summary": result.summary,
                "replayed": len(replays),
                "recurred_with_change": sum(r[0] != "resolved" for r in replays),
                "later": later,
            }
        )
    return found


# Outcomes by stage (11.6, 7.5)

# Rejected reviewer operations other than candidates: reflection's refusals.
REFLECTION = ("knowledge", "assignment", "pattern_merge", "item_merge")


def stage_outcomes(conn: sqlite3.Connection, run_id: str) -> dict:
    """What a dream's evidence gathering, reflection, candidate selection and
    adoption came to, with the reason for each deferral, as counts and short
    reasons rather than any session's text."""

    def count(sql: str, *args: object) -> int:
        return conn.execute(sql, (run_id, *args)).fetchone()[0]

    def by_status(table: str) -> Counter:
        return Counter(
            dict(
                conn.execute(
                    f"SELECT status, COUNT(*) FROM {table} WHERE run = ?"
                    " GROUP BY status",
                    (run_id,),
                ).fetchall()
            )
        )

    reads = Counter(
        dict(
            conn.execute(
                "SELECT state, COUNT(*) FROM steps WHERE run = ? AND phase = 'read'"
                " GROUP BY state",
                (run_id,),
            ).fetchall()
        )
    )
    candidates, changes = by_status("candidates"), by_status("changes")
    deferred = [
        {"stage": "selection", "subject": row["summary"], "reason": row["reason"]}
        for row in conn.execute(
            "SELECT summary, reason FROM candidates WHERE run = ?"
            " AND status IN ('deferred', 'discarded') ORDER BY created_at, id",
            (run_id,),
        )
    ]
    deferred += [
        {
            "stage": "adoption",
            "subject": row["summary"] or row["id"],
            "reason": row["reason"],
        }
        for row in conn.execute(
            "SELECT id, summary, reason FROM changes WHERE run = ?"
            " AND status IN ('waiting', 'withheld', 'failed') AND reason IS NOT NULL"
            " ORDER BY created_at, id",
            (run_id,),
        )
    ]
    return {
        "evidence_gathering": {
            "sessions_read": reads["done"],
            "sessions_skipped": reads["skipped"],
            "sessions_failed": reads["failed"],
            "observations_accepted": count(
                "SELECT COUNT(*) FROM observations WHERE run = ?"
            ),
            "observations_rejected": count(
                "SELECT COUNT(*) FROM rejections WHERE run = ? AND what = ?",
                "observation",
            ),
        },
        "reflection": {
            "patterns_created": count(
                "SELECT COUNT(*) FROM patterns WHERE created_run = ?"
            ),
            "knowledge_revisions": count(
                "SELECT COUNT(*) FROM knowledge_revisions WHERE originating_run = ?"
            ),
            "operations_rejected": count(
                "SELECT COUNT(*) FROM rejections WHERE run = ?"
                f" AND what IN {REFLECTION}"
            ),
        },
        "selection": {
            "candidates": sum(candidates.values()),
            "authored": candidates["authored"],
            "deferred": candidates["deferred"],
            "discarded": candidates["discarded"],
            "rejected": count(
                "SELECT COUNT(*) FROM rejections WHERE run = ? AND what = ?",
                "candidate",
            ),
        },
        "adoption": {
            status: changes[status]
            for status in ("applied", "waiting", "withheld", "failed")
        },
        "deferred": deferred,
    }


def describe(found: Effect) -> str:
    """An effect in words for the report."""
    if found.insufficient:
        return f"insufficient evidence: {found.insufficient}"
    text = f"rate {found.before_rate:.2f} before, {found.after_rate:.2f} after"
    if found.recurring:
        text += "; recurs at or above its earlier rate, so it is a candidate again"
    return text


def observation_counts(observations: int, count: int) -> str:
    return f"{_plural(observations, 'observation')} in {_plural(count, 'session')}"

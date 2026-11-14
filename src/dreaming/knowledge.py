"""Knowledge support, ranking signals and forgetting (2.11, 4.5, 11.3, 13.3).

Every count, ranking, rate and replay query reads observations through
`COUNTED_OBSERVATIONS`, which leaves out evidence dismissed by feedback (13.5)
and every observation of a forgotten session (2.11). The forgotten flag is read
at query time, so support lost to a forgotten session is withheld at once and
returns when the session is restored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from dreaming import store
from dreaming.errors import USAGE_ERROR, DreamingError

# Observation kinds that matter to the rules here (13.1).
USER_STATEMENT = "user_statement"
TOOL_PROMPT = "tool_prompt"  # a prompt written by a tool or an unknown author (2.9)
CONFIRMED_OUTCOMES = ("user_confirmed", "check_confirmed")

# Knowledge kinds that claim an approach worked, which needs a confirmed success
# in scope rather than recurrence alone (13.3).
APPROACH_KINDS = ("workflow",)

# Observations neither dismissed as evidence nor from a forgotten session, each
# with the interaction it belongs to: one interaction counts once, whichever
# source copies it was read through (13.6).
COUNTED_OBSERVATIONS = """
SELECT o.*, COALESCE(s.thread_id, o.source || ':' || o.session_id) AS interaction
FROM observations o
LEFT JOIN sessions_seen s ON s.source = o.source AND s.session_id = o.session_id
WHERE o.excluded = 0 AND COALESCE(s.forgotten, 0) = 0
"""


def counted_observations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The observations that may count as support or be passed to a role."""
    return conn.execute(
        f"{COUNTED_OBSERVATIONS} ORDER BY o.observed_on, o.evidence_id"
    ).fetchall()


# Ranking signals (11.3)


@dataclass(frozen=True)
class Signals:
    observations: int
    sessions: int  # distinct interactions
    projects: int
    last_date: str | None


def pattern_signals(conn: sqlite3.Connection) -> dict[str, Signals]:
    rows = conn.execute(
        "SELECT pattern_id, COUNT(*) AS observations,"
        " COUNT(DISTINCT interaction) AS sessions,"
        " COUNT(DISTINCT project) AS projects, MAX(observed_on) AS last_date"
        f" FROM ({COUNTED_OBSERVATIONS})"
        " WHERE pattern_id IS NOT NULL GROUP BY pattern_id"
    )
    return {
        row["pattern_id"]: Signals(
            row["observations"], row["sessions"], row["projects"], row["last_date"]
        )
        for row in rows
    }


def rank_patterns(
    signals: Mapping[str, Signals], cfg: dict, today: date
) -> list[tuple[str, float]]:
    """Patterns by `w_sessions·sessions + w_recency·2^(-age/half_life) +
    w_projects·projects`, highest first, ties broken by id."""
    weights = cfg["ranking"]

    def score(s: Signals) -> float:
        recency = 0.0
        if s.last_date:
            age = max((today - date.fromisoformat(s.last_date[:10])).days, 0)
            half_life = weights["half_life_days"]
            recency = 2 ** (-age / half_life) if half_life else float(age == 0)
        return (
            weights["w_sessions"] * s.sessions
            + weights["w_recency"] * recency
            + weights["w_projects"] * s.projects
        )

    scored = [(pattern, score(s)) for pattern, s in signals.items()]
    return sorted(scored, key=lambda item: (-item[1], item[0]))


# Support (4.5, 13.3)


@dataclass(frozen=True)
class Support:
    sessions: int = 0  # distinct interactions
    user_statements: int = 0  # asserted, so explicit (13.3)
    confirmed_successes: int = 0
    file_findings: int = 0


def evidence_support(conn: sqlite3.Connection, evidence_ids: Sequence[str]) -> Support:
    """What the counted part of this evidence supplies."""
    ids = json.dumps(list(evidence_ids))
    row = conn.execute(
        "SELECT COUNT(DISTINCT interaction) AS sessions,"
        " COUNT(*) FILTER (WHERE kind = ? AND modality = 'asserted') AS statements,"
        f" COUNT(*) FILTER (WHERE outcome IN {CONFIRMED_OUTCOMES}) AS successes"
        f" FROM ({COUNTED_OBSERVATIONS})"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))",
        (USER_STATEMENT, ids),
    ).fetchone()
    files = conn.execute(
        "SELECT COUNT(*) FROM file_findings"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))",
        (ids,),
    ).fetchone()[0]
    return Support(row["sessions"], row["statements"], row["successes"], files)


def support_holds(support: Support, cfg: dict, kind: str | None = None) -> bool:
    """Whether evidence meets 4.5 and 13.3.

    A file finding stands on its own (4.5) and an explicit user statement applies
    without recurrence (13.3); anything else needs `min_sessions` distinct
    interactions. A claim that an approach worked also needs a confirmed success.
    """
    if kind in APPROACH_KINDS and not support.confirmed_successes:
        return False
    return bool(
        support.file_findings
        or support.user_statements
        or support.sessions >= cfg["thresholds"]["min_sessions"]
    )


def _not_found(what: str, identifier: str) -> DreamingError:
    return DreamingError(f"unknown {what} {identifier}", USAGE_ERROR, "not_found")


def item_withheld(conn: sqlite3.Connection, item_id: str, cfg: dict) -> bool:
    """Whether default retrieval and adoption hold the item back: the counted
    part of its supporting evidence fails 4.5 or 13.3. Forgetting a session
    withholds what it alone held up at once, and restoring it releases it (2.11).
    """
    row = conn.execute(
        "SELECT r.kind, r.supporting FROM knowledge_items k JOIN knowledge_revisions r"
        " ON r.item_id = k.id AND r.version = k.version WHERE k.id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise _not_found("knowledge item", item_id)
    support = evidence_support(conn, json.loads(row["supporting"]))
    return not support_holds(support, cfg, row["kind"])


def change_withheld(conn: sqlite3.Connection, change_id: str, cfg: dict) -> bool:
    """As `item_withheld`, for a proposed repository change (2.11)."""
    row = conn.execute("SELECT evidence FROM changes WHERE id = ?", (change_id,))
    row = row.fetchone()
    if row is None:
        raise _not_found("change", change_id)
    return not support_holds(evidence_support(conn, json.loads(row["evidence"])), cfg)


# Reassessment (2.10, 2.11, 11.7)


def affected_items(
    conn: sqlite3.Connection, sessions: Iterable[tuple[str, str]]
) -> list[str]:
    """Knowledge items whose current revision cites evidence from these sessions."""
    evidence: set[str] = set()
    for source, session_id in sessions:
        rows = conn.execute(
            "SELECT evidence_id FROM observations WHERE source = ? AND session_id = ?",
            (source, session_id),
        )
        evidence.update(row[0] for row in rows)
    if not evidence:
        return []
    rows = conn.execute(
        "SELECT r.item_id, r.supporting, r.contradicting FROM knowledge_items k"
        " JOIN knowledge_revisions r ON r.item_id = k.id AND r.version = k.version"
        " ORDER BY r.item_id"
    )
    return [
        row["item_id"]
        for row in rows
        if evidence
        & {*json.loads(row["supporting"]), *json.loads(row["contradicting"])}
    ]


def mark_pending(conn: sqlite3.Connection, item_ids: Iterable[str]) -> None:
    """Queue items for the next in-scope dream's reassessment; retrieval shows the
    mark until reassessment records its conclusion (2.10)."""
    conn.executemany(
        "UPDATE knowledge_items SET pending_reassessment = 1 WHERE id = ?",
        [(item_id,) for item_id in item_ids],
    )


# Forgetting (2.11)


def session_copies(
    conn: sqlite3.Connection,
    session_id: str,
    source: str | None = None,
    correlation_id: str | None = None,
) -> list[sqlite3.Row]:
    """Every stored source copy of a session: the named one and those sharing its
    thread id or `correlation_id` (2.4). A thread id names the session too."""
    return conn.execute(
        """
        SELECT source, session_id, forgotten FROM sessions_seen
        WHERE (session_id = :id AND (:source IS NULL OR source = :source))
           OR thread_id IN (:id, :correlation)
           OR thread_id IN (
               SELECT thread_id FROM sessions_seen
               WHERE session_id = :id AND (:source IS NULL OR source = :source))
        ORDER BY source, session_id
        """,
        {"id": session_id, "source": source, "correlation": correlation_id},
    ).fetchall()


def _set_forgotten(
    conn: sqlite3.Connection, session_id: str, source: str | None, forgotten: bool
) -> dict:
    with store.transaction(conn):
        copies = session_copies(conn, session_id, source)
        if not copies:
            raise _not_found("session", session_id)
        sessions = [(row["source"], row["session_id"]) for row in copies]
        changed = any(bool(row["forgotten"]) != forgotten for row in copies)
        items: list[str] = []
        if changed:
            conn.executemany(
                "UPDATE sessions_seen SET forgotten = ?"
                " WHERE source = ? AND session_id = ?",
                [(int(forgotten), *session) for session in sessions],
            )
            items = affected_items(conn, sessions)
            mark_pending(conn, items)
    return {
        "session": session_id,
        "state": "forgotten" if forgotten else "active",
        "changed": changed,
        "copies": [list(session) for session in sessions],
        "reassess": items,
    }


def forget_session(
    conn: sqlite3.Connection, session_id: str, source: str | None = None
) -> dict:
    """Exclude a session and every copy of it from selection, registration and
    counting, keeping its data, and queue the knowledge it supported (2.11)."""
    return _set_forgotten(conn, session_id, source, True)


def restore_session(
    conn: sqlite3.Connection, session_id: str, source: str | None = None
) -> dict:
    """Make a forgotten session count again and queue its knowledge again (2.11)."""
    return _set_forgotten(conn, session_id, source, False)

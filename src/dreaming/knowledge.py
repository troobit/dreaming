"""Knowledge rules: support, promotion, scope, conflicts, revisions, ranking,
learning dispositions and forgetting (2.10, 2.11, 4.5, 11.3, 13.x, 15.3).

Every count, ranking, rate and replay query reads observations through
`COUNTED_OBSERVATIONS`, which leaves out evidence dismissed by feedback (13.5)
and every observation of a forgotten session (2.11). The forgotten flag is read
at query time, so support lost to a forgotten session is withheld at once and
returns when the session is restored.

The rules adapt concepts from OpenClaw's dreaming and the design's Prior Art
sources with no copied source (12.2): staged candidates before active items,
recurrence gates, operations rather than prose, append-only revisions with the
actor and base version, a cap on how much one run may rewrite, and conflicts
kept as `contested` instead of resolved by the model.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from dreaming import config, store
from dreaming.errors import USAGE_ERROR, DreamingError, Rejected

# Observation kinds that matter to the rules here (13.1).
USER_STATEMENT = "user_statement"
TOOL_PROMPT = "tool_prompt"  # a prompt written by a tool or an unknown author (2.9)
CONFIRMED_OUTCOMES = ("user_confirmed", "check_confirmed")

# Knowledge kinds that claim an approach worked, which needs a confirmed success
# in scope rather than recurrence alone (13.3).
APPROACH_KINDS = ("workflow",)
# Kinds supported only by what the user said: help requests or a stated goal,
# never tool failures or assistant mistakes (15.1).
LEARNING_KINDS = ("learning",)
CONFIDENCE = ("low", "medium", "high")

# Observations neither dismissed as evidence nor from a forgotten session, each
# with the interaction it belongs to: one interaction counts once, whichever
# source copies it was read through (13.6). Retrieval shows these as evidence
# references (14.2).
SHOWN_OBSERVATIONS = """
SELECT o.*, COALESCE(s.thread_id, o.source || ':' || o.session_id) AS interaction
FROM observations o
LEFT JOIN sessions_seen s ON s.source = o.source AND s.session_id = o.session_id
WHERE o.excluded = 0 AND COALESCE(s.forgotten, 0) = 0
"""
# Those that count as support: imported evidence never counts toward this
# store's promotion thresholds (14.3).
COUNTED_OBSERVATIONS = SHOWN_OBSERVATIONS + "AND o.imported = 0\n"


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


def recency(s: Signals, cfg: dict, today: date) -> float:
    """`2^(-age_days / half_life_days)` of the latest observation; 0 without one."""
    if not s.last_date:
        return 0.0
    age = max((today - date.fromisoformat(s.last_date[:10])).days, 0)
    half_life = cfg["ranking"]["half_life_days"]
    return 2 ** (-age / half_life) if half_life else float(age == 0)


def score(s: Signals, cfg: dict, today: date) -> float:
    weights = cfg["ranking"]
    return (
        weights["w_sessions"] * s.sessions
        + weights["w_recency"] * recency(s, cfg, today)
        + weights["w_projects"] * s.projects
    )


def rank_patterns(
    signals: Mapping[str, Signals], cfg: dict, today: date
) -> list[tuple[str, float]]:
    """Patterns by `w_sessions·sessions + w_recency·2^(-age/half_life) +
    w_projects·projects`, highest first, ties broken by id."""
    scored = [(pattern, score(s, cfg, today)) for pattern, s in signals.items()]
    return sorted(scored, key=lambda item: (-item[1], item[0]))


# Support (4.5, 13.3)


@dataclass(frozen=True)
class Support:
    sessions: int = 0  # distinct interactions
    user_statements: int = 0  # asserted, so explicit (13.3)
    confirmed_successes: int = 0
    file_findings: int = 0


def evidence_support(
    conn: sqlite3.Connection, evidence_ids: Sequence[str], *, user_only: bool = False
) -> Support:
    """What the counted part of this evidence supplies; with `user_only`, only
    user statements count (15.1)."""
    ids = json.dumps(list(evidence_ids))
    row = conn.execute(
        "SELECT COUNT(DISTINCT interaction) AS sessions,"
        " COUNT(*) FILTER (WHERE kind = :user AND modality = 'asserted')"
        " AS statements,"
        f" COUNT(*) FILTER (WHERE outcome IN {CONFIRMED_OUTCOMES}) AS successes"
        f" FROM ({COUNTED_OBSERVATIONS})"
        " WHERE evidence_id IN (SELECT value FROM json_each(:ids))"
        " AND (:user_only = 0 OR kind = :user)",
        {"user": USER_STATEMENT, "ids": ids, "user_only": int(user_only)},
    ).fetchone()
    files = conn.execute(
        "SELECT COUNT(*) FROM file_findings WHERE imported = 0"
        " AND evidence_id IN (SELECT value FROM json_each(?))",
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


def split_evidence(
    conn: sqlite3.Connection,
    kind: str,
    supporting: Iterable[str],
    contradicting: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Supporting and contradicting evidence, sorted. For a claim that an approach
    worked, a confirmed failure cited in support counts against it (13.3)."""
    supporting, contradicting = set(supporting), set(contradicting)
    if kind in APPROACH_KINDS:
        rows = conn.execute(
            "SELECT evidence_id FROM observations WHERE outcome = 'failed'"
            " AND evidence_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(supporting)),),
        )
        failed = {row[0] for row in rows}
        supporting -= failed
        contradicting |= failed
    return sorted(supporting), sorted(contradicting)


def item_support(conn: sqlite3.Connection, kind: str, supporting: Sequence[str]):
    """The support an item of this kind draws from its supporting evidence."""
    return evidence_support(conn, supporting, user_only=kind in LEARNING_KINDS)


def _not_found(what: str, identifier: str) -> DreamingError:
    return DreamingError(f"unknown {what} {identifier}", USAGE_ERROR, "not_found")


def corrected_by_user(conn: sqlite3.Connection, item_id: str) -> bool:
    """Whether the user corrected the item directly (13.5); the correction is
    the user's own word, which applies at once and which inferred evidence
    never overrides (13.3, 13.4)."""
    row = conn.execute(
        "SELECT 1 FROM knowledge_revisions WHERE item_id = ? AND actor = 'user'",
        (item_id,),
    ).fetchone()
    return row is not None


def revision_withheld(conn: sqlite3.Connection, row: Mapping, cfg: dict) -> bool:
    """Whether default retrieval and adoption hold a revision back: the counted
    part of its supporting evidence fails 4.5 or 13.3 and the user has not
    corrected the item. Forgetting a session withholds what it alone held up at
    once, and restoring it releases it (2.11)."""
    if corrected_by_user(conn, row["item_id"]):
        return False
    supporting, _ = split_evidence(conn, row["kind"], json.loads(row["supporting"]))
    support = item_support(conn, row["kind"], supporting)
    return not support_holds(support, cfg, row["kind"])


def item_withheld(conn: sqlite3.Connection, item_id: str, cfg: dict) -> bool:
    """`revision_withheld` for an item's current revision."""
    row = current(conn, item_id)
    if row is None:
        raise _not_found("knowledge item", item_id)
    return revision_withheld(conn, row, cfg)


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
    return items_citing(conn, evidence)


def items_citing(conn: sqlite3.Connection, evidence: Iterable[str]) -> list[str]:
    """Knowledge items whose current revision cites any of this evidence."""
    evidence = set(evidence)
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


# Knowledge items and their revisions (13.2-13.9)


def new_id(prefix: str, *parts: object) -> str:
    """A deterministic identifier, so applying the same stored answer again after
    a crash makes the same records."""
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode())
    return f"{prefix}-{digest.hexdigest()[:12]}"


def current(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    """An item's current revision with the item's own fields."""
    return conn.execute(
        "SELECT r.*, k.pattern_id, k.pending_reassessment, k.imported"
        " FROM knowledge_items k JOIN knowledge_revisions r"
        " ON r.item_id = k.id AND r.version = k.version WHERE k.id = ?",
        (item_id,),
    ).fetchone()


# A run's revisions are retained once it has ended; until then they are a running
# dream's proposals (14.2). Revisions from feedback or an import have no run of
# this store's and are retained at once.
RETAINED_RUNS = ("complete", "partial", "budget_exhausted")


def retained(
    conn: sqlite3.Connection, item_id: str | None = None
) -> dict[str, sqlite3.Row]:
    """Each item's latest retained revision, with the item's own fields; with
    `item_id`, that item's only."""
    rows = conn.execute(
        "SELECT r.*, k.pattern_id, k.pending_reassessment, k.imported"
        " FROM knowledge_revisions r JOIN knowledge_items k ON k.id = r.item_id"
        " LEFT JOIN runs u ON u.id = r.originating_run"
        f" WHERE (r.originating_run IS NULL OR u.status IN {RETAINED_RUNS})"
        " AND (:item IS NULL OR r.item_id = :item)"
        " ORDER BY r.item_id, r.version",
        {"item": item_id},
    )
    return {row["item_id"]: row for row in rows}


def evidence_refs(
    conn: sqlite3.Connection,
    ids: Iterable[str],
    quote_chars: int,
    *,
    counted: bool = False,
) -> list[dict]:
    """Evidence by id as shown outside the store, never as transcript: an
    observation's redacted quote cut to `quote_chars`, with its modality so a
    hedge reads as a hedge, or a file finding with where it was found; each with
    its source's availability (13.7). Evidence dismissed by feedback or from a
    forgotten session is never shown (2.11, 13.5); with `counted`, imported
    evidence is left out too."""
    ids = json.dumps(sorted(set(ids)))
    query = COUNTED_OBSERVATIONS if counted else SHOWN_OBSERVATIONS
    refs = []
    for row in conn.execute(
        f"SELECT * FROM ({query})"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))",
        (ids,),
    ):
        quote = row["quote"]
        if len(quote) > quote_chars:
            quote = quote[:quote_chars] + "…"
        refs.append(
            {
                "id": row["evidence_id"],
                "quote": quote,
                "kind": row["kind"],
                "modality": row["modality"],
                "outcome": row["outcome"],
                "date": row["observed_on"],
                "project": row["project"],
                "availability": row["availability"],
            }
        )
    for row in conn.execute(
        "SELECT * FROM file_findings WHERE (:all OR imported = 0)"
        " AND evidence_id IN (SELECT value FROM json_each(:ids))",
        {"all": int(not counted), "ids": ids},
    ):
        refs.append(
            {
                "id": row["evidence_id"],
                "repo": row["repo"],
                "revision": row["revision"],
                "path": row["path"],
                "location": row["location"],
                "finding": row["finding"],
                "availability": row["availability"],
            }
        )
    return sorted(refs, key=lambda ref: ref["id"])


def same_text(text: str) -> str:
    """Item content as compared for equivalence (13.9)."""
    return " ".join(text.lower().split()).rstrip(" .")


@dataclass(frozen=True)
class Judgement:
    """The status the rules give an item for its evidence (4.5, 13.3, 13.4)."""

    status: str
    support: Support
    against: Support
    supporting: list[str]
    contradicting: list[str]


def judge(
    conn: sqlite3.Connection,
    cfg: dict,
    kind: str,
    supporting: Iterable[str],
    contradicting: Iterable[str] = (),
) -> Judgement:
    """Contested when counted evidence conflicts and no explicit user statement
    settles it; active when the support meets 4.5 and 13.3; a candidate
    otherwise. Promotion depends only on the evidence set, never its order."""
    supporting, contradicting = split_evidence(conn, kind, supporting, contradicting)
    support = item_support(conn, kind, supporting)
    against = evidence_support(conn, contradicting)
    settled = support.user_statements > 0 and against.user_statements == 0
    if (against.sessions or against.file_findings) and not settled:
        status = "contested"
    elif support_holds(support, cfg, kind):
        status = "active"
    else:
        status = "candidate"
    return Judgement(status, support, against, supporting, contradicting)


def confidence_cap(judgement: Judgement, cfg: dict) -> str:
    """The highest confidence the counts allow: `high` needs many distinct
    sessions, a confirmed outcome or the user's own statement and no
    contradiction; `medium` needs support that meets 4.5 (design, Retrieval)."""
    s = judgement.support
    least = cfg["thresholds"]["min_sessions"]
    contradicted = judgement.against.sessions or judgement.against.file_findings
    if not contradicted and (
        s.sessions >= 2 * least or s.confirmed_successes or s.user_statements
    ):
        return "high"
    if s.sessions >= least or s.user_statements or s.confirmed_successes:
        return "medium"
    return "medium" if s.file_findings else "low"


# Scope (13.2, 13.4)

SCOPE_LISTS = ("domains", "projects", "workflows")


def scope_key(scope: Mapping) -> str:
    return json.dumps(dict(scope), sort_keys=True)


def _context(conn: sqlite3.Connection, evidence_ids: Sequence[str]) -> dict:
    """The domains and projects the counted evidence comes from."""
    ids = json.dumps(list(evidence_ids))
    rows = conn.execute(
        f"SELECT domain, project FROM ({COUNTED_OBSERVATIONS})"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))"
        " UNION SELECT domain, project FROM file_findings"
        " WHERE evidence_id IN (SELECT value FROM json_each(?))",
        (ids, ids),
    ).fetchall()
    return {
        "domains": sorted({row["domain"] for row in rows if row["domain"]}),
        "projects": sorted({row["project"] for row in rows if row["project"]}),
    }


def stated(raw: Mapping | None) -> bool:
    """Whether a scope was stated at all; a missing scope is never global."""
    return bool(
        raw
        and (
            raw.get("global")
            or raw.get("unresolved")
            or any(raw.get(k) for k in SCOPE_LISTS)
        )
    )


def resolve_scope(
    conn: sqlite3.Connection,
    raw: Mapping | None,
    supporting: Sequence[str],
    *,
    by_user: bool = False,
) -> dict:
    """The scope to store: the stated one, checked, or the evidence's own domains
    and projects when none is stated; unresolved when those are unknown. A
    global scope needs evidence from more than one domain or the user's own
    statement or correction (`by_user`), so recurrence in one scope never makes
    an item global (13.4)."""
    scope = {key: sorted(set((raw or {}).get(key) or [])) for key in SCOPE_LISTS}
    scope["global"] = bool((raw or {}).get("global"))
    scope["unresolved"] = bool((raw or {}).get("unresolved"))
    if scope["global"] and scope["unresolved"]:
        raise Rejected("a scope cannot be both global and unresolved")
    if scope["global"]:
        if any(scope[key] for key in SCOPE_LISTS):
            raise Rejected("a global scope names no domains, projects or workflows")
        domains = _context(conn, supporting)["domains"]
        stated_by_user = by_user or evidence_support(conn, supporting).user_statements
        if len(domains) < 2 and not stated_by_user:
            raise Rejected(
                "a global scope needs evidence from more than one domain or the "
                "user's own statement (13.4)"
            )
    elif not stated(raw):
        scope.update(_context(conn, supporting))
        scope["unresolved"] = not any(scope[key] for key in SCOPE_LISTS)
    return scope


def scope_applies(
    scope: Mapping,
    cfg: dict,
    *,
    domain: str | None = None,
    project: str | None = None,
    workflow: str | None = None,
) -> bool:
    """Whether an item applies to a query: every dimension it constrains must be
    supplied and match, a project implying its domain. Global items always
    apply; unresolved items and items with no scope never do (13.2, 14.2)."""
    if scope.get("unresolved"):
        return False
    if scope.get("global"):
        return True
    if project and not domain:
        domain = config.assign_domain(cfg, "", project)
    given = {"domains": domain, "projects": project, "workflows": workflow}
    constrained = [key for key in SCOPE_LISTS if scope.get(key)]
    return bool(constrained) and all(given[key] in scope[key] for key in constrained)


def retrieval_key(item: Mapping) -> tuple:
    """Retrieval order: the most specific scope first (workflow, project, domain,
    global), then confidence, the latest supporting date and the id, so the
    order never depends on the order items were read in (14.2)."""
    scope = item["scope"]
    specificity = next(
        (rank for rank, key in enumerate(reversed(SCOPE_LISTS)) if scope.get(key)),
        len(SCOPE_LISTS),
    )
    latest = item.get("latest_support_date")
    age = -date.fromisoformat(latest[:10]).toordinal() if latest else 0
    return (specificity, -CONFIDENCE.index(item["confidence"]), age, item["id"])


# Applying operations (13.2-13.9, design Review and knowledge)


def counted_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    """The ids that name counted evidence: an observation neither dismissed nor
    from a forgotten session, or a file finding."""
    return {
        row[0]
        for row in conn.execute(
            f"SELECT evidence_id FROM ({COUNTED_OBSERVATIONS})"
            " WHERE evidence_id IN (SELECT value FROM json_each(:ids))"
            " UNION SELECT evidence_id FROM file_findings WHERE imported = 0"
            " AND evidence_id IN (SELECT value FROM json_each(:ids))",
            {"ids": json.dumps(sorted(set(ids)))},
        )
    }


def check_evidence(conn: sqlite3.Connection, ids: Iterable[str]) -> None:
    """Every id must name counted evidence (`counted_ids`)."""
    ids = sorted(set(ids))
    found = counted_ids(conn, ids)
    missing = [i for i in ids if i not in found]
    if missing:
        raise Rejected(
            "cites evidence that is unknown, dismissed or from a forgotten "
            f"session: {', '.join(missing[:5])}"
        )


class Reviser:
    """Applies knowledge operations under the rules, inside the caller's
    transaction, raising `Rejected` with the reason for any it refuses.

    Operations carry the fields of the reviewer's schema, with `pattern_id` in
    place of `pattern`. Each applied operation appends a revision with its
    actor, base version and originating run or feedback, and records the
    conclusion of any pending reassessment (2.10, 11.7). A stale base version is
    refused and the item queued for the next review.

    With the `user` actor the operation is the user's own correction through
    feedback (13.5): it applies at once with the stated confidence, makes the
    item active unless it dismisses it, may give it a global scope, and settles
    the item against inferred evidence from then on (13.3, 13.4).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: dict,
        *,
        now: str,
        run_id: str | None = None,
        feedback_id: str | None = None,
        actor: str = "dream",
        backend: str | None = None,
        model: str | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.now = now
        self.run_id = run_id
        self.feedback_id = feedback_id
        self.actor = actor
        self.backend = backend
        self.model = model
        self.removed: dict[str, int] = {}
        self.active: dict[str, int] = {}
        for row in conn.execute(
            "SELECT scope FROM knowledge_items WHERE status = 'active'"
        ):
            key = scope_key(json.loads(row["scope"]))
            self.active[key] = self.active.get(key, 0) + 1

    def apply(self, op: Mapping, seed: object) -> str:
        """Apply one operation and return the item it created or changed; `seed`
        makes a created item's id deterministic."""
        handlers = {
            "create": self._create,
            "revise": self._revise,
            "supersede": self._supersede,
            "contest": self._contest,
            "dismiss": self._dismiss,
            "confirm": self._confirm,
        }
        return handlers[op["op"]](op, seed)

    # Checks

    def _evidence(self, ids: Iterable[str]) -> None:
        check_evidence(self.conn, ids)

    def _existing(self, op: Mapping) -> sqlite3.Row:
        row = current(self.conn, op["item_id"]) if op["item_id"] else None
        if row is None:
            raise Rejected(f"no knowledge item {op['item_id']}")
        if op["base_version"] != row["version"]:
            mark_pending(self.conn, [row["item_id"]])
            raise Rejected(
                f"item {row['item_id']} is at version {row['version']}, not the "
                f"{op['base_version']} read; queued for the next review"
            )
        if row["status"] == "dismissed":
            raise Rejected(
                f"item {row['item_id']} is dismissed and is not reactivated "
                "without the user's instruction (13.5)"
            )
        if row["status"] == "superseded":
            raise Rejected(f"item {row['item_id']} is superseded")
        return row

    def user_settled(self, row: Mapping) -> bool:
        """Whether an item rests on the user's own explicit statement or
        correction, which inferred evidence never overrides (13.4)."""
        statements = evidence_support(
            self.conn, json.loads(row["supporting"])
        ).user_statements
        return statements > 0 or corrected_by_user(self.conn, row["item_id"])

    def _cap(self, row: Mapping) -> None:
        """A run may supersede or dismiss at most `max_revision_fraction` of the
        active items in one scope (at least one); beyond that the item is
        queued for the next review."""
        if self.actor != "dream":
            return
        key = scope_key(json.loads(row["scope"]))
        active = self.active.get(key, 0)
        allowed = max(1, int(self.cfg["thresholds"]["max_revision_fraction"] * active))
        used = self.removed.get(key, 0)
        if used >= allowed:
            mark_pending(self.conn, [row["item_id"]])
            raise Rejected(
                f"this dream already superseded or dismissed {used} of {active} "
                "active items in this scope (max_revision_fraction); queued for "
                "the next review"
            )
        self.removed[key] = used + 1

    def _equivalent(self, content: str, scope: Mapping) -> sqlite3.Row | None:
        """An item stating the same interpretation in the same scope (13.9)."""
        wanted, key = same_text(content), scope_key(scope)
        rows = self.conn.execute(
            "SELECT r.*, k.pattern_id FROM knowledge_items k JOIN knowledge_revisions r"
            " ON r.item_id = k.id AND r.version = k.version ORDER BY k.id"
        )
        for row in rows:
            if (
                same_text(row["content"]) == wanted
                and scope_key(json.loads(row["scope"])) == key
            ):
                return row
        return None

    # Writing

    def _write(
        self,
        item_id: str,
        *,
        version: int,
        base_version: int | None,
        kind: str,
        content: str,
        status: str,
        scope: Mapping,
        confidence: str,
        basis: str,
        supporting: Sequence[str],
        contradicting: Sequence[str],
        pattern_id: str | None,
        reason: str,
    ) -> None:
        ids = json.dumps(list(supporting))
        latest = self.conn.execute(
            "SELECT MAX(day) FROM (SELECT observed_on AS day"
            f" FROM ({COUNTED_OBSERVATIONS})"
            " WHERE evidence_id IN (SELECT value FROM json_each(:ids))"
            " UNION ALL SELECT found_on FROM file_findings"
            " WHERE evidence_id IN (SELECT value FROM json_each(:ids)))",
            {"ids": ids},
        ).fetchone()[0]
        lens = lens_version = None
        if pattern_id:
            lens = self.conn.execute(
                "SELECT lens FROM patterns WHERE id = ?", (pattern_id,)
            ).fetchone()[0]
            lens_version = self.conn.execute(
                "SELECT MAX(lens_version) FROM observations WHERE lens = ?"
                " AND evidence_id IN (SELECT value FROM json_each(?))",
                (lens, ids),
            ).fetchone()[0]
        scope_json = scope_key(scope)
        if version == 1:
            self.conn.execute(
                "INSERT INTO knowledge_items (id, version, kind, status, scope,"
                " summary, pattern_id, created_at, updated_at)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    kind,
                    status,
                    scope_json,
                    content,
                    pattern_id,
                    self.now,
                    self.now,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE knowledge_items SET version = ?, kind = ?, status = ?,"
                " scope = ?, summary = ?, pattern_id = COALESCE(?, pattern_id),"
                " pending_reassessment = 0, updated_at = ? WHERE id = ?",
                (
                    version,
                    kind,
                    status,
                    scope_json,
                    content,
                    pattern_id,
                    self.now,
                    item_id,
                ),
            )
        self.conn.execute(
            "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
            " scope, confidence, confidence_basis, supporting, contradicting,"
            " latest_support_date, lens, lens_version, backend, model,"
            " originating_run, feedback_id, actor, base_version, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                version,
                content,
                kind,
                status,
                scope_json,
                confidence,
                basis,
                ids,
                json.dumps(list(contradicting)),
                latest,
                lens,
                lens_version,
                self.backend,
                self.model,
                self.run_id,
                self.feedback_id,
                self.actor,
                base_version,
                reason,
                self.now,
            ),
        )

    def _judged(
        self,
        op: Mapping,
        kind: str,
        supporting: Iterable[str],
        contradicting: Iterable[str],
    ) -> tuple[Judgement, str, str]:
        """The judgement, and the stated confidence capped by the counts."""
        judgement = judge(self.conn, self.cfg, kind, supporting, contradicting)
        cap = confidence_cap(judgement, self.cfg)
        confidence, basis = op["confidence"], op["confidence_basis"]
        if self.actor == "user":
            return judgement, confidence, basis
        if CONFIDENCE.index(confidence) > CONFIDENCE.index(cap):
            s = judgement.support
            basis = (
                f"{basis} (capped at {cap}: {s.sessions} sessions, "
                f"{s.user_statements} user statements, "
                f"{s.confirmed_successes} confirmed outcomes)"
            ).strip()
            confidence = cap
        return judgement, confidence, basis

    def _store(
        self,
        item_id: str,
        op: Mapping,
        *,
        version: int,
        base_version: int | None,
        kind: str,
        content: str,
        scope: Mapping,
        supporting: Iterable[str],
        contradicting: Iterable[str],
        pattern_id: str | None,
        reason: str,
        status: str | None = None,
    ) -> Judgement:
        judgement, confidence, basis = self._judged(op, kind, supporting, contradicting)
        if status is None:
            status = "active" if self.actor == "user" else judgement.status
        self._write(
            item_id,
            version=version,
            base_version=base_version,
            kind=kind,
            content=content,
            status=status,
            scope=scope,
            confidence=confidence,
            basis=basis,
            supporting=judgement.supporting,
            contradicting=judgement.contradicting,
            pattern_id=pattern_id,
            reason=reason,
        )
        return judgement

    # Operations

    def _create(
        self, op: Mapping, seed: object, *, scope: Mapping | None = None
    ) -> str:
        content = op["content"].strip()
        if not content:
            raise Rejected("an item needs content")
        if not op["supporting"]:
            raise Rejected("an item needs supporting evidence")
        self._evidence([*op["supporting"], *op["contradicting"]])
        if scope is None:
            scope = resolve_scope(self.conn, op["scope"], op["supporting"])
        same = self._equivalent(content, scope)
        if same is not None:
            if same["status"] in ("dismissed", "superseded"):
                raise Rejected(
                    f"states {same['status']} item {same['item_id']} again in the "
                    "same scope (13.5, 13.9)"
                )
            # An equivalent interpretation revises the existing item (13.9).
            return self._revise(
                {**op, "item_id": same["item_id"], "base_version": same["version"]},
                seed,
            )
        item_id = new_id("k", self.run_id, self.feedback_id, seed, content)
        self._store(
            item_id,
            op,
            version=1,
            base_version=None,
            kind=op["kind"],
            content=content,
            scope=scope,
            supporting=op["supporting"],
            contradicting=op["contradicting"],
            pattern_id=op.get("pattern_id"),
            reason=op["reason"],
        )
        return item_id

    def _next(
        self,
        row: Mapping,
        op: Mapping,
        *,
        reason: str,
        status: str | None = None,
        content: str | None = None,
        scope: Mapping | None = None,
    ) -> str:
        """Append the item's next revision. Evidence accumulates: the revision
        keeps what earlier revisions cited and adds the operation's."""
        self._evidence([*op["supporting"], *op["contradicting"]])
        self._store(
            row["item_id"],
            op,
            version=row["version"] + 1,
            base_version=op.get("base_version"),
            kind=op["kind"],
            content=content or row["content"],
            scope=scope or json.loads(row["scope"]),
            supporting=[*json.loads(row["supporting"]), *op["supporting"]],
            contradicting=[*json.loads(row["contradicting"]), *op["contradicting"]],
            pattern_id=op.get("pattern_id"),
            reason=reason,
            status=status,
        )
        return row["item_id"]

    def _revise(self, op: Mapping, seed: object) -> str:
        row = self._existing(op)
        scope = None
        if stated(op["scope"]):
            scope = resolve_scope(
                self.conn,
                op["scope"],
                op["supporting"],
                by_user=self.actor == "user",
            )
        return self._next(
            row, op, reason=op["reason"], content=op["content"].strip(), scope=scope
        )

    def _supersede(self, op: Mapping, seed: object) -> str:
        row = self._existing(op)
        self._evidence([*op["supporting"], *op["contradicting"]])
        if not evidence_support(self.conn, op["supporting"]).user_statements:
            raise Rejected(
                "only the user's explicit correction supersedes an item (13.4)"
            )
        scope = json.loads(row["scope"])
        if stated(op["scope"]):
            scope = resolve_scope(self.conn, op["scope"], op["supporting"])
        if same_text(op["content"]) == same_text(row["content"]) and scope_key(
            scope
        ) == scope_key(json.loads(row["scope"])):
            raise Rejected("a replacement must state a different interpretation")
        self._cap(row)
        replacement = self._create(
            {**op, "reason": f"supersedes {row['item_id']}: {op['reason']}"},
            seed,
            scope=scope,
        )
        self._next(
            row,
            {
                **op,
                "kind": row["kind"],
                "confidence": row["confidence"],
                "confidence_basis": row["confidence_basis"],
                "supporting": [],
                "contradicting": [],
            },
            reason=f"superseded by {replacement}: {op['reason']}",
            status="superseded",
        )
        return replacement

    def _contest(self, op: Mapping, seed: object) -> str:
        row = self._existing(op)
        if not op["contradicting"]:
            raise Rejected("contesting an item needs contradicting evidence")
        self._evidence(op["contradicting"])
        if (
            self.user_settled(row)
            and not evidence_support(self.conn, op["contradicting"]).user_statements
        ):
            raise Rejected(
                f"item {row['item_id']} rests on the user's explicit statement, "
                "which inferred evidence does not contest (13.4)"
            )
        return self._next(row, {**op, "kind": row["kind"]}, reason=op["reason"])

    def _dismiss(self, op: Mapping, seed: object) -> str:
        row = self._existing(op)
        cited = [*op["supporting"], *op["contradicting"]]
        self._evidence(cited)
        if (
            self.actor != "user"
            and self.user_settled(row)
            and not evidence_support(self.conn, cited).user_statements
        ):
            raise Rejected(
                f"item {row['item_id']} rests on the user's explicit statement; "
                "only the user dismisses it (13.4, 13.5)"
            )
        self._cap(row)
        return self._next(
            row, {**op, "kind": row["kind"]}, reason=op["reason"], status="dismissed"
        )

    def _confirm(self, op: Mapping, seed: object) -> str:
        """Re-validation (11.7): the item is judged again on its evidence, and the
        revision records whether it still holds."""
        row = self._existing(op)
        if row["status"] != "active":
            raise Rejected(f"item {row['item_id']} is {row['status']}, not active")
        return self._next(row, {**op, "kind": row["kind"]}, reason=op["reason"])

    def merge(self, item_ids: Sequence[str], into: str, reason: str) -> str:
        """Merge items stating the same interpretation in the same scope into one,
        which keeps every item's evidence; the others are superseded by it."""
        ids = list(dict.fromkeys(item_ids))
        if into not in ids or len(ids) < 2:
            raise Rejected("a merge names at least two items, including the one kept")
        rows = {}
        for item_id in ids:
            row = current(self.conn, item_id)
            if row is None:
                raise Rejected(f"no knowledge item {item_id}")
            if row["status"] in ("dismissed", "superseded"):
                raise Rejected(f"item {item_id} is {row['status']}")
            rows[item_id] = row
        if len({scope_key(json.loads(r["scope"])) for r in rows.values()}) > 1:
            raise Rejected("merged items must share one scope")
        kept, others = rows[into], [rows[i] for i in ids if i != into]
        if not self.user_settled(kept) and any(map(self.user_settled, others)):
            raise Rejected(
                "an item resting on the user's explicit statement is kept, not "
                "merged into one that does not (13.4)"
            )
        op = {
            "kind": kept["kind"],
            "confidence": kept["confidence"],
            "confidence_basis": kept["confidence_basis"],
            "supporting": [e for r in others for e in json.loads(r["supporting"])],
            "contradicting": [
                e for r in others for e in json.loads(r["contradicting"])
            ],
            "base_version": kept["version"],
        }
        names = ", ".join(r["item_id"] for r in others)
        self._next(kept, op, reason=f"merged {names}: {reason}")
        for row in others:
            self._next(
                row,
                {
                    **op,
                    "kind": row["kind"],
                    "supporting": [],
                    "contradicting": [],
                    "base_version": row["version"],
                },
                reason=f"merged into {into}: {reason}",
                status="superseded",
            )
        return into


# Learning recommendations (15.3)

DISPOSITIONS = ("open", "dismissed", "deferred", "addressed")


def learning_disposition(row: Mapping, today: date) -> str:
    """A recommendation's disposition today: a deferral that has ended makes it
    open again; one with no date lasts until the user resumes it."""
    until = row["deferred_until"]
    if (
        row["disposition"] == "deferred"
        and until
        and date.fromisoformat(until[:10]) <= today
    ):
        return "open"
    return row["disposition"]


def set_learning_disposition(
    conn: sqlite3.Connection,
    learning_id: str,
    disposition: str,
    *,
    now: str,
    until: str | None = None,
    reason: str | None = None,
) -> dict:
    """The user dismisses, defers (until a date, or until resumed), resumes
    (`open`) or marks addressed a learning recommendation. Nothing else changes
    a disposition: delivering an explanation or a pattern going quiet never
    marks it addressed (15.3)."""
    if disposition not in DISPOSITIONS:
        raise DreamingError(
            f"disposition must be one of {', '.join(DISPOSITIONS)}",
            USAGE_ERROR,
            "invalid_request",
        )
    if until is not None:
        if disposition != "deferred":
            raise DreamingError(
                "only a deferral takes a date", USAGE_ERROR, "invalid_request"
            )
        try:
            date.fromisoformat(until[:10])
        except ValueError as exc:
            raise DreamingError(
                f"{until!r} is not an ISO 8601 date", USAGE_ERROR, "invalid_request"
            ) from exc
    cursor = conn.execute(
        "UPDATE learning SET disposition = ?, deferred_until = ?, reason = ?,"
        " updated_at = ? WHERE id = ?",
        (disposition, until, reason, now, learning_id),
    )
    if cursor.rowcount == 0:
        raise _not_found("learning recommendation", learning_id)
    return {"id": learning_id, "disposition": disposition, "deferred_until": until}

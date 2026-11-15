"""`dream api`: versioned, machine-readable operations (14.1-14.3, 14.5-14.7).

One JSON request on stdin, one JSON response on stdout. Every message carries
`{"dreaming_api": 1, "op": ...}`; a response adds `ok` and, on error, a stable
`code` with the message. Nothing here needs an agentic-coding checkout, an
interactive harness or a browser (14.4). The knowledge operations take no lock:
SQLite's WAL mode covers their writes while a dream runs.

Retrieval returns retained revisions only; a running dream's revisions are
proposals until it ends (`knowledge.retained`). By default it returns active
knowledge that applies to the query's scope and still has its support, and
learning recommendations that are open; `include` asks for the rest. Items are
ordered by scope specificity, confidence, latest support and id, and added
until the next would exceed the limit in estimated tokens, `utf8_bytes/4` of
the item's compact JSON.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import IO

from dreaming import config, exchange, knowledge, store
from dreaming.backends import estimate_tokens
from dreaming.errors import SUCCESS, USAGE_ERROR, DreamingError, Rejected

VERSION = 1
ESTIMATOR = "utf8_bytes/4"
# Small on purpose: injecting more knowledge does not make an agent better
# (design, Retrieval and operations).
DEFAULT_LIMIT = 1000
SCOPE_KEYS = ("domain", "project", "workflow")
INCLUDE = (
    "candidate",
    "contested",
    "superseded",
    "dismissed",
    "unresolved",
    "out_of_scope",
    "withheld",
    "deferred",
    "addressed",
)
OUTCOMES = ("success", "failure", "unknown")
BASES = ("user_confirmed", "check_confirmed", "unknown")
USES = ("cited", "present_only")


def _invalid(message: str, code: str = "invalid_request") -> DreamingError:
    return DreamingError(message, USAGE_ERROR, code)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _cut(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[:chars] + "…"


# Reading requests


def _text(request: Mapping, key: str, *, required: bool = True) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} must be a non-empty string")
    return value


def _flag(request: Mapping, key: str) -> bool:
    value = request.get(key, False)
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false")
    return value


def _query(request: Mapping) -> dict:
    """The scope a retrieval asks about: any of a domain, project and workflow."""
    raw = request.get("scope")
    if not isinstance(raw, dict) or set(raw) - set(SCOPE_KEYS):
        raise _invalid(
            f"scope must be an object with any of {', '.join(SCOPE_KEYS)}",
            "invalid_scope",
        )
    query = {}
    for key in SCOPE_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise _invalid(f"scope.{key} must be a non-empty string", "invalid_scope")
        query[key] = value
    if not query:
        raise _invalid(
            "the scope needs at least one of domain, project and workflow",
            "invalid_scope",
        )
    return query


def _limit(request: Mapping) -> int:
    limit = request.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise _invalid("limit must be a positive number of estimated tokens")
    return limit


def _include(request: Mapping) -> set[str]:
    include = request.get("include", [])
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        raise _invalid("include must be a list of names")
    unknown = sorted(set(include) - set(INCLUDE))
    if unknown:
        raise _invalid(f"include takes {', '.join(INCLUDE)}, not {', '.join(unknown)}")
    return set(include)


# Retrieval (14.2, 14.6, 14.7)


def _research(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """Research behind a learning recommendation, kept apart from the evidence
    about the user (15.2): each finding with its sources' titles and links."""
    rows = conn.execute(
        "SELECT * FROM research_findings"
        " WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(ids),),
    )
    return [
        {
            "id": row["id"],
            "claim": row["finding"],
            "sources": [
                {key: source[key] for key in ("title", "link", "retrieved_on", "kind")}
                for source in json.loads(row["sources"])
            ],
        }
        for row in rows
    ]


class _Library:
    """Retained knowledge items and learning recommendations as retrieval
    shows them."""

    def __init__(self, conn: sqlite3.Connection, cfg: dict, today) -> None:
        self.conn = conn
        self.cfg = cfg
        self.today = today
        self.quote_chars = cfg["thresholds"]["quote_chars"]

    def item(self, row: Mapping, *, evidence: bool = True) -> dict:
        entry = {
            "type": "knowledge",
            "id": row["item_id"],
            "revision": row["version"],
            "kind": row["kind"],
            "status": row["status"],
            "content": row["content"],
            "scope": json.loads(row["scope"]),
            "confidence": row["confidence"],
            "confidence_basis": row["confidence_basis"],
            "latest_support_date": row["latest_support_date"],
            "pending_reassessment": bool(row["pending_reassessment"]),
            "imported": bool(row["imported"]),
            # Imported items keep their stated status; their evidence never
            # counts here, so support is not judged again (14.3).
            "withheld": not row["imported"]
            and knowledge.revision_withheld(self.conn, row, self.cfg),
        }
        if evidence:
            entry["evidence"] = {
                side: knowledge.evidence_refs(
                    self.conn, json.loads(row[side]), self.quote_chars
                )
                for side in ("supporting", "contradicting")
            }
        return entry

    def learning(self, row: Mapping, *, evidence: bool = True) -> dict:
        cited = json.loads(row["evidence"])
        judgement = knowledge.judge(self.conn, self.cfg, "learning", cited)
        support = judgement.support
        refs = knowledge.evidence_refs(self.conn, cited, self.quote_chars)
        entry = {
            "type": "learning_recommendation",
            "id": row["id"],
            "revision": None,
            "kind": "learning",
            "status": knowledge.learning_disposition(row, self.today),
            "content": row["summary"],
            "next_step": row["next_step"],
            "helps_achieve": row["outcome"],
            "deferred_until": row["deferred_until"],
            "scope": json.loads(row["scope"]),
            "confidence": knowledge.confidence_cap(judgement, self.cfg),
            "confidence_basis": (
                f"{support.user_statements} explicit requests or goals in "
                f"{support.sessions} sessions"
            ),
            "latest_support_date": max(
                (ref["date"] for ref in refs if ref.get("date")), default=None
            ),
            "pending_reassessment": False,
            "imported": bool(row["imported"]),
            "withheld": not row["imported"]
            and not knowledge.support_holds(support, self.cfg, "learning"),
        }
        if evidence:
            entry["evidence"] = {"supporting": refs, "contradicting": []}
            entry["research"] = _research(self.conn, json.loads(row["research"]))
        return entry

    def learning_rows(self, learning_id: str | None = None) -> list[sqlite3.Row]:
        """Recommendations from ended runs (or none), like retained revisions."""
        return self.conn.execute(
            "SELECT l.* FROM learning l LEFT JOIN runs u ON u.id = l.run"
            f" WHERE (l.run IS NULL OR u.status IN {knowledge.RETAINED_RUNS})"
            " AND (:id IS NULL OR l.id = :id) ORDER BY l.id",
            {"id": learning_id},
        ).fetchall()

    def select(self, query: dict, include: set[str], *, evidence: bool) -> list[dict]:
        """What a retrieval returns before the limit, in retrieval order."""

        def shown(status: str, default: str, scope: Mapping) -> bool:
            if status != default and status not in include:
                return False
            if knowledge.scope_applies(scope, self.cfg, **query):
                return True
            return ("unresolved" if scope.get("unresolved") else "out_of_scope") in (
                include
            )

        found = []
        for row in knowledge.retained(self.conn).values():
            if shown(row["status"], "active", json.loads(row["scope"])):
                found.append(self.item(row, evidence=evidence))
        for row in self.learning_rows():
            status = knowledge.learning_disposition(row, self.today)
            if shown(status, "open", json.loads(row["scope"])):
                found.append(self.learning(row, evidence=evidence))
        found = [e for e in found if not e["withheld"] or "withheld" in include]
        return sorted(found, key=knowledge.retrieval_key)


def _within(entries: list[dict], limit: int) -> dict:
    """The leading entries that fit the limit, with the estimate stated."""
    kept, used = [], 0
    for entry in entries:
        size = estimate_tokens(_json(entry))
        if used + size > limit:
            break
        kept.append(entry)
        used += size
    return {
        "items": kept,
        "limit": limit,
        "estimator": ESTIMATOR,
        "estimated_tokens": used,
        "omitted_count": len(entries) - len(kept),
    }


def recall(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    """Applicable knowledge for a scope within a limit (14.2)."""
    query, include, limit = _query(request), _include(request), _limit(request)
    library = _Library(conn, cfg, now.date())
    return _within(library.select(query, include, evidence=True), limit)


def search(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    """Applicable knowledge containing every query term, as summaries (14.6)."""
    terms = _text(request, "query").lower().split()
    query, include, limit = _query(request), _include(request), _limit(request)
    library = _Library(conn, cfg, now.date())
    chars = cfg["thresholds"]["quote_chars"]
    found = [
        {
            **{key: entry[key] for key in entry if key != "content"},
            "summary": _cut(entry["content"], chars),
        }
        for entry in library.select(query, include, evidence=False)
        if all(term in entry["content"].lower() for term in terms)
    ]
    return _within(found, limit)


def get(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    """One item's current retained revision, whatever its status (14.7)."""
    item_id = _text(request, "id")
    library = _Library(conn, cfg, now.date())
    row = knowledge.retained(conn, item_id).get(item_id)
    if row is not None:
        return {"item": library.item(row)}
    rows = library.learning_rows(item_id)
    if rows:
        return {"item": library.learning(rows[0])}
    raise DreamingError(
        f"no retained knowledge item {item_id}", USAGE_ERROR, "not_found"
    )


# Feedback (11.7, 13.5, 14.5, 15.3)


def _revisions(request: Mapping) -> list[dict]:
    raw = request.get("revisions", [])
    if not isinstance(raw, list):
        raise _invalid("revisions must be a list")
    revisions = []
    for cited in raw:
        if (
            not isinstance(cited, dict)
            or not isinstance(cited.get("item_id"), str)
            or isinstance(cited.get("version"), bool)
            or not isinstance(cited.get("version"), int)
        ):
            raise _invalid("each revision names its item_id and version")
        if cited.get("use") not in USES:
            raise _invalid(f"each revision's use is one of {', '.join(USES)}")
        revisions.append(
            {
                "item_id": cited["item_id"],
                "version": cited["version"],
                "use": cited["use"],
            }
        )
    return revisions


def _correction(raw: object) -> dict | None:
    """The user's correction of an item: new content, a new scope, or a
    dismissal that may also exclude the evidence it rested on (13.5)."""
    if raw is None:
        return None
    version = raw.get("base_version") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("item_id"), str)
        or isinstance(version, bool)
        or not isinstance(version, int)
    ):
        raise _invalid("a correction names the item_id and the base_version read")
    content = _text(raw, "content", required=False)
    scope = raw.get("scope")
    if scope is not None and not isinstance(scope, dict):
        raise _invalid("a correction's scope is an object")
    dismiss = _flag(raw, "dismiss")
    if not (content or scope or dismiss):
        raise _invalid("a correction changes the content or scope, or dismisses")
    return {
        "item_id": raw["item_id"],
        "base_version": version,
        "content": content,
        "scope": scope,
        "dismiss": dismiss,
        "exclude_evidence": _flag(raw, "exclude_evidence"),
        "reason": _text(raw, "reason", required=False) or "the user's correction",
    }


def _disposition(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _invalid("learning must be an object")
    return {
        "id": _text(raw, "id"),
        "disposition": _text(raw, "disposition"),
        "until": _text(raw, "until", required=False),
        "reason": _text(raw, "reason", required=False),
    }


def _correct(
    conn: sqlite3.Connection, cfg: dict, feedback_id: str, c: dict, stamp: str
) -> tuple[dict, set[str]]:
    """Apply the user's correction as a revision by the user, and return the
    revision and the other items queued because their evidence was excluded."""
    row = knowledge.current(conn, c["item_id"])
    if row is None:
        raise DreamingError(
            f"no knowledge item {c['item_id']}", USAGE_ERROR, "not_found"
        )
    if c["base_version"] != row["version"]:
        raise DreamingError(
            f"item {c['item_id']} is at version {row['version']}, not "
            f"{c['base_version']}; read it again",
            USAGE_ERROR,
            "stale_version",
        )
    excluded: list[str] = []
    if c["dismiss"] and c["exclude_evidence"]:
        excluded = [
            found[0]
            for found in conn.execute(
                "SELECT evidence_id FROM observations"
                " WHERE evidence_id IN (SELECT value FROM json_each(?))",
                (row["supporting"],),
            )
        ]
        conn.executemany(
            "UPDATE observations SET excluded = 1 WHERE evidence_id = ?",
            [(evidence_id,) for evidence_id in excluded],
        )
    op = {
        "op": "dismiss" if c["dismiss"] else "revise",
        "item_id": row["item_id"],
        "base_version": row["version"],
        "kind": row["kind"],
        "content": c["content"] or row["content"],
        "scope": c["scope"],
        "confidence": row["confidence"] if c["dismiss"] else "high",
        "confidence_basis": row["confidence_basis"]
        if c["dismiss"]
        else f"corrected by the user (feedback {feedback_id})",
        "supporting": [],
        "contradicting": [],
        "pattern_id": None,
        "reason": c["reason"],
    }
    reviser = knowledge.Reviser(
        conn, cfg, now=stamp, feedback_id=feedback_id, actor="user"
    )
    try:
        reviser.apply(op, feedback_id)
    except Rejected as rejected:
        raise DreamingError(rejected.reason, USAGE_ERROR, "rejected") from None
    others = set(knowledge.items_citing(conn, excluded)) - {row["item_id"]}
    knowledge.mark_pending(conn, others)
    version = knowledge.current(conn, row["item_id"])["version"]
    return {"item_id": row["item_id"], "version": version}, others


def feedback(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    """Record a consuming tool's feedback: the revisions behind an action, each
    cited or only present in the prompt, with the outcome and its basis; and
    the user's corrections and learning dispositions. A feedback id already
    recorded changes nothing (14.5). Feedback is never evidence about the user;
    the items it names are queued for the next in-scope dream (11.7)."""
    feedback_id = _text(request, "feedback_id")
    revisions = _revisions(request)
    outcome = request.get("outcome")
    if outcome is not None and outcome not in OUTCOMES:
        raise _invalid(f"outcome is one of {', '.join(OUTCOMES)} or null")
    basis = request.get("basis", "unknown")
    if basis not in BASES:
        raise _invalid(f"basis is one of {', '.join(BASES)}")
    check = _text(request, "check", required=basis == "check_confirmed")
    run_ref = _text(request, "run_ref", required=False)
    session_ref = _text(request, "session_ref", required=False)
    correction = _correction(request.get("correction"))
    disposition = _disposition(request.get("learning"))
    if outcome is not None and not revisions:
        raise _invalid("an outcome names the revisions behind the action")
    if not revisions and correction is None and disposition is None:
        raise _invalid(
            "feedback needs revisions, a correction or a learning disposition"
        )
    stamp = now.isoformat(timespec="seconds")
    with store.transaction(conn):
        if conn.execute(
            "SELECT 1 FROM feedback WHERE id = ?", (feedback_id,)
        ).fetchone():
            return {"feedback_id": feedback_id, "changed": False}
        for cited in revisions:
            found = conn.execute(
                "SELECT 1 FROM knowledge_revisions WHERE item_id = ? AND version = ?",
                (cited["item_id"], cited["version"]),
            ).fetchone()
            if found is None:
                raise DreamingError(
                    f"no revision {cited['version']} of knowledge item "
                    f"{cited['item_id']}",
                    USAGE_ERROR,
                    "not_found",
                )
        actions = None
        if correction or disposition:
            actions = json.dumps({"correction": correction, "learning": disposition})
        conn.execute(
            "INSERT INTO feedback (id, revisions, run_ref, session_ref, outcome,"
            " basis, check_name, correction, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feedback_id,
                json.dumps(revisions),
                run_ref,
                session_ref,
                outcome,
                basis,
                check,
                actions,
                stamp,
            ),
        )
        reassess = {cited["item_id"] for cited in revisions}
        knowledge.mark_pending(conn, sorted(reassess))
        revised = None
        if correction is not None:
            revised, others = _correct(conn, cfg, feedback_id, correction, stamp)
            reassess = (reassess | others) - {correction["item_id"]}
        learning = None
        if disposition is not None:
            learning = knowledge.set_learning_disposition(
                conn,
                disposition["id"],
                disposition["disposition"],
                now=stamp,
                until=disposition["until"],
                reason=disposition["reason"],
            )
    return {
        "feedback_id": feedback_id,
        "changed": True,
        "reassess": sorted(reassess),
        "revision": revised,
        "learning": learning,
    }


# Exchange (14.3)


def export(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    return {"export": exchange.export(conn)}


def import_(conn: sqlite3.Connection, cfg: dict, request: Mapping, now) -> dict:
    return exchange.import_(conn, request.get("export"))


# Requests and responses

OPS: dict[str, Callable[[sqlite3.Connection, dict, Mapping, datetime], dict]] = {
    "knowledge.recall": recall,
    "knowledge.search": search,
    "knowledge.get": get,
    "knowledge.feedback": feedback,
    "knowledge.export": export,
    "knowledge.import": import_,
}


def _error(op: object, exc: DreamingError) -> tuple[dict, int]:
    response = {
        "dreaming_api": VERSION,
        "op": op,
        "ok": False,
        "code": exc.code,
        "error": exc.message,
    }
    return response, exc.exit_code


def _respond(
    conn: sqlite3.Connection, cfg: dict, request: object, now: datetime
) -> tuple[dict, int]:
    op = request.get("op") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise _invalid("a request is a JSON object")
        version = request.get("dreaming_api")
        if version is None:
            raise _invalid(
                f'a request names its version: {{"dreaming_api": {VERSION}}}'
            )
        if isinstance(version, bool) or version != VERSION:
            raise _invalid(
                f"this dream speaks dreaming_api {VERSION}, not {version}",
                "incompatible_version",
            )
        if not isinstance(op, str):
            raise _invalid("a request names its op")
        handler = OPS.get(op)
        if handler is None:
            raise _invalid(f"unknown op {op!r}; ops are {', '.join(OPS)}", "unknown_op")
        result = handler(conn, cfg, request, now)
    except DreamingError as exc:
        return _error(op, exc)
    return {"dreaming_api": VERSION, "op": op, "ok": True, **result}, SUCCESS


def handle(
    conn: sqlite3.Connection,
    cfg: dict,
    request: object,
    *,
    now: datetime | None = None,
) -> dict:
    """The response to one request."""
    response, _ = _respond(conn, cfg, request, now or datetime.now(UTC))
    return response


def main(stdin: IO[str], stdout: IO[str]) -> int:
    """`dream api`: read one request, write one response, and exit 0 when the
    operation succeeded, or with its error's exit code."""
    try:
        request = json.loads(stdin.read())
    except json.JSONDecodeError as exc:
        response, code = _error(None, _invalid(f"the request is not JSON: {exc}"))
    else:
        try:
            home = config.ensure_home()
            cfg = config.load_config(home)
            conn = store.open_store(store.store_path(home))
        except DreamingError as exc:
            op = request.get("op") if isinstance(request, dict) else None
            response, code = _error(op, exc)
        else:
            try:
                response, code = _respond(conn, cfg, request, datetime.now(UTC))
            finally:
                conn.close()
    stdout.write(_json(response) + "\n")
    stdout.flush()
    return code

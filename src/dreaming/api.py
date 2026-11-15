"""Versioned local knowledge operations shared by the CLI and other consumers.

Requests use top-level fields beside ``dreaming_api`` and ``op``. Feedback
references are ``{item_id, version, usage: cited|present_only}``; exchange uses
the ``export`` field in both directions. No operation calls a model.
"""

import json
import math
import sqlite3
from datetime import UTC, datetime

from dreaming.config import Config
from dreaming.exchange import export_knowledge, import_knowledge
from dreaming.knowledge import (
    _rows,
    _support_sufficient,
    available_observations,
    knowledge_for_use,
    learning_for_use,
    normalise_scope,
    queue_reassessment,
    set_learning_disposition,
)
from dreaming.observe import canonical
from dreaming.redact import redact
from dreaming.sessions import _timestamp
from dreaming.store import Store

STATUSES = {"candidate", "active", "contested", "superseded", "dismissed"}
INCLUDES = STATUSES | {"unresolved", "addressed", "deferred_until", "currency_notes"}


class APIError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise APIError("invalid_request", f"{name} must be an integer >= {minimum}")
    return value


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise APIError("invalid_request", f"{name} must be nonempty text")
    return value


def _query_scope(config, request):
    scope = request.get("scope", {})
    if not isinstance(scope, dict) or scope.keys() - {"domain", "project", "workflow"}:
        raise APIError("invalid_scope", "Scope accepts domain, project and workflow")
    scope = {**scope, **{k: request[k] for k in ("domain", "project", "workflow") if k in request}}
    if any(not isinstance(v, str) or not v for v in scope.values()):
        raise APIError("invalid_scope", "Scope values must be nonempty strings")
    if "project" in scope and "domain" not in scope:
        domain = config.domain("", scope["project"])
        if domain:
            scope["domain"] = domain
    return scope


def _matches(scope, query, include):
    scope = normalise_scope(scope)
    if scope["unresolved"]:
        return "unresolved" in include
    if scope["global"]:
        return True
    if not any(scope[d] for d in ("domains", "projects", "workflows")):
        return False
    return all(
        not scope[dimension] or query.get(field) in scope[dimension]
        for dimension, field in (
            ("domains", "domain"),
            ("projects", "project"),
            ("workflows", "workflow"),
        )
    )


def _evidence(store, ids, quote_chars):
    wanted = set(ids)
    result = []
    for row in available_observations(store, evidence_ids=list(wanted)):
        result.append(
            {
                "id": row["id"],
                "kind": "observation",
                "source": row["source"],
                "session_id": row["session_id"],
                "location": row["source_location"],
                "representation_version": row["representation_version"],
                "quote": row["redacted_quote"][:quote_chars],
                "modality": row["modality"],
                "availability": row["availability"],
            }
        )
    for row in _rows(store.connection.execute("SELECT * FROM file_findings ORDER BY id")):
        if row["id"] in wanted:
            result.append(
                {
                    "id": row["id"],
                    "kind": "file_finding",
                    "repo": row["repo"],
                    "revision": row["revision"],
                    "path": row["path"],
                    "location": row["location"],
                    "quote": row["finding"][:quote_chars],
                    "modality": "asserted",
                    "availability": row["availability"],
                }
            )
    return sorted(result, key=lambda row: row["id"])


def _item(store, row, quote_chars):
    result = {
        key: row[key]
        for key in (
            "id",
            "version",
            "kind",
            "status",
            "summary",
            "confidence",
            "confidence_basis",
            "latest_support_at",
            "lens_id",
            "model",
            "run_id",
            "feedback_id",
        )
    }
    result["scope"] = json.loads(row["scope"])
    result["pending_reassessment"] = bool(row["pending_reassessment"])
    result["imported"] = bool(row["imported"])
    for field in ("supporting_evidence", "contradicting_evidence"):
        ids = row[field] if isinstance(row[field], list) else json.loads(row[field])
        references = _evidence(store, ids, quote_chars)
        result[field] = [ref["id"] for ref in references]
        result["evidence" if field == "supporting_evidence" else "counterevidence"] = references
    return result


def _recommendation(store, row, quote_chars):
    return {
        "id": row["id"],
        "kind": "learning",
        "scope": json.loads(row["scope"]),
        "status": row["disposition"],
        "summary": row["next_step"],
        "outcome": row["outcome"],
        "deferred_until": row["deferred_until"],
        "reason": row["reason"],
        "evidence": _evidence(store, json.loads(row["evidence"]), quote_chars),
    }


def _order(item):
    scope = item["scope"]
    specificity = next(
        (
            value
            for dimension, value in (("workflows", 3), ("projects", 2), ("domains", 1))
            if scope.get(dimension)
        ),
        0,
    )
    date = _timestamp(item.get("latest_support_at"))
    return (
        -specificity,
        -{"high": 3, "medium": 2, "low": 1}.get(item.get("confidence"), 0),
        -date.timestamp() if date else math.inf,
        item["id"],
    )


def _retrieve(store, config, request):
    query_scope = _query_scope(config, request)
    include = request.get("include", [])
    if not isinstance(include, list) or any(
        not isinstance(x, str) or x not in INCLUDES for x in include
    ):
        raise APIError("invalid_request", "include contains an unknown status or collection")
    include = set(include)
    limit = _integer(request.get("limit", 1024), "limit")
    quote_chars = _integer(request.get("quote_chars", 240), "quote_chars")
    live = {row["id"]: row for row in knowledge_for_use(store, config)}
    items = []
    for row in _rows(store.connection.execute("SELECT * FROM knowledge_items ORDER BY id")):
        if row["status"] == "active":
            if row["id"] not in live:
                continue
            row = live[row["id"]]
        elif row["status"] not in include:
            continue
        if _matches(json.loads(row["scope"]), query_scope, include):
            items.append(_item(store, row, quote_chars))
    eligible_learning = {row["id"] for row in learning_for_use(store)}
    for row in _rows(store.connection.execute("SELECT * FROM learning ORDER BY id")):
        if row["id"] not in eligible_learning and row["disposition"] not in include:
            continue
        if _matches(json.loads(row["scope"]), query_scope, include):
            item = _recommendation(store, row, quote_chars)
            remaining = {ref["id"] for ref in item["evidence"]}
            lost_support = set(json.loads(row["evidence"])) - remaining
            if (
                lost_support
                and row["disposition"] not in include
                and not _support_sufficient(
                    store,
                    json.loads(row["evidence"]),
                    config,
                    kind="learning",
                    scope=json.loads(row["scope"]),
                )
            ):
                continue
            if item["evidence"] or row["disposition"] in include:
                items.append(item)
    if "currency_notes" in include:
        for row in _rows(store.connection.execute("SELECT * FROM currency_findings ORDER BY id")):
            if row["status"] != "note":
                continue
            scope = {"projects": [row["target"]]}
            if _matches(scope, query_scope, include):
                items.append(
                    {
                        "id": row["id"],
                        "kind": "currency_note",
                        "scope": scope,
                        "summary": row["claim"],
                        "status": row["status"],
                        "sources": json.loads(row["sources"]),
                    }
                )
    if request["op"] == "knowledge.search":
        terms = _required_text(request.get("query"), "query").casefold().split()
        items = [
            item for item in items if all(term in item["summary"].casefold() for term in terms)
        ]
    items.sort(key=_order)
    selected = []
    tokens = 0
    for item in items:
        estimate = math.ceil(len(canonical([*selected, item]).encode("utf-8")) / 4)
        if estimate > limit:
            break
        selected.append(item)
        tokens = estimate
    return {
        "items": selected,
        "estimated_tokens": tokens,
        "estimator": "utf8_bytes/4",
        "omitted_count": len(items) - len(selected),
    }


def _get(store, request):
    item_id = _required_text(request.get("id"), "id")
    rows = _rows(store.connection.execute("SELECT * FROM knowledge_items WHERE id=?", (item_id,)))
    if not rows:
        raise APIError("not_found", f"Unknown knowledge item {item_id}")
    return {"item": _item(store, rows[0], _integer(request.get("quote_chars", 240), "quote_chars"))}


def _feedback(store, request):
    feedback_id = _required_text(request.get("feedback_id"), "feedback_id")
    if store.connection.execute("SELECT 1 FROM feedback WHERE id=?", (feedback_id,)).fetchone():
        return {"feedback_id": feedback_id, "changed": False}
    refs = request.get("revisions", [])
    if not isinstance(refs, list):
        raise APIError("invalid_request", "revisions must be a list")
    for flag in ("dismiss", "exclude_evidence"):
        if flag in request and type(request[flag]) is not bool:
            raise APIError("invalid_request", f"{flag} must be a boolean")
    for field in ("run_ref", "session_ref", "outcome", "reason", "check_name"):
        if request.get(field) is not None and not isinstance(request[field], str):
            raise APIError("invalid_request", f"{field} must be text")
    targets = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("usage") not in {"cited", "present_only"}:
            raise APIError("invalid_request", "Each revision needs item_id, version and usage")
        item_id = _required_text(ref.get("item_id"), "item_id")
        version = _integer(ref.get("version"), "version", 1)
        if not store.connection.execute(
            "SELECT 1 FROM knowledge_revisions WHERE item_id=? AND version=?", (item_id, version)
        ).fetchone():
            raise APIError("not_found", f"Unknown retained revision {item_id}/{version}")
        targets.append(
            _rows(store.connection.execute("SELECT * FROM knowledge_items WHERE id=?", (item_id,)))[
                0
            ]
        )
    basis = request.get("basis", "unknown")
    if basis not in {"user-confirmed", "check-confirmed", "unknown"}:
        raise APIError("invalid_request", "Unknown outcome basis")
    if basis == "check-confirmed":
        _required_text(request.get("check_name"), "check_name")
    changes = (
        request.get("correction") is not None or "scope" in request or request.get("dismiss", False)
    )
    if changes and len(refs) != 1:
        raise APIError(
            "invalid_request", "A correction, scope change or dismissal targets one revision"
        )
    if request.get("correction") is not None:
        _required_text(request["correction"], "correction")
    if "scope" in request:
        normalise_scope(request["scope"])
    if changes and targets[0]["version"] != refs[0]["version"]:
        store.connection.execute(
            "UPDATE knowledge_items SET pending_reassessment=1 WHERE id=?", (targets[0]["id"],)
        )
        raise APIError("stale_version", "The corrected knowledge revision has changed")
    if request.get("dismiss"):
        _required_text(request.get("reason"), "reason")
    now = datetime.now(UTC).isoformat()
    with store.transaction() as conn:
        # Check under the write transaction so concurrent clients cannot insert twice.
        if conn.execute("SELECT 1 FROM feedback WHERE id=?", (feedback_id,)).fetchone():
            return {"feedback_id": feedback_id, "changed": False}
        conn.execute(
            "INSERT INTO feedback(id,revisions,run_ref,session_ref,outcome,basis,check_name,"
            "correction,scope,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                feedback_id,
                canonical(refs),
                request.get("run_ref"),
                request.get("session_ref"),
                request.get("outcome"),
                basis,
                request.get("check_name"),
                redact(request["correction"]) if request.get("correction") is not None else None,
                canonical(normalise_scope(request["scope"])) if "scope" in request else None,
                redact(request["reason"]) if request.get("reason") else None,
                now,
            ),
        )
        for target in targets:
            conn.execute(
                "UPDATE knowledge_items SET pending_reassessment=1 WHERE id=?", (target["id"],)
            )
        if changes:
            _correct(conn, targets[0], request, feedback_id, now)
        if "learning_id" in request:
            set_learning_disposition(
                store,
                request["learning_id"],
                request.get("disposition"),
                until=request.get("deferred_until"),
                reason=request.get("reason"),
            )
    return {"feedback_id": feedback_id, "changed": True}


def _correct(conn, old, request, feedback_id, now):
    # User feedback is a retained correction, not another independent observation.
    values = dict(old)
    values.update(
        version=old["version"] + 1,
        base_version=old["version"],
        actor="feedback",
        feedback_id=feedback_id,
        pending_reassessment=0,
        user_resolved=1,
        run_id=None,
        model=None,
    )
    if request.get("correction") is not None:
        values.update(summary=redact(request["correction"]), status="active")
    if "scope" in request:
        values["scope"] = canonical(normalise_scope(request["scope"]))
    if request.get("dismiss"):
        values["status"] = "dismissed"
    if request.get("reason"):
        values["confidence_basis"] = redact(request["reason"])
    cursor = conn.execute(
        "UPDATE knowledge_items SET "
        + ",".join(k + "=?" for k in values if k != "id")
        + " WHERE id=? AND version=?",
        (*[v for k, v in values.items() if k != "id"], old["id"], old["version"]),
    )
    if not cursor.rowcount:
        raise APIError("stale_version", "The corrected knowledge revision has changed")
    values.pop("id")
    values.pop("pattern_id")
    values.pop("imported")
    values.update(item_id=old["id"], created_at=now)
    conn.execute(
        f"INSERT INTO knowledge_revisions({','.join(values)}) "
        f"VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    if request.get("dismiss") and request.get("exclude_evidence"):
        evidence = set(json.loads(old["supporting_evidence"]))
        for evidence_id in evidence:
            conn.execute("UPDATE observations SET excluded=1 WHERE id=?", (evidence_id,))
        # The target remains dismissed; other items sharing its evidence are reassessed.
        queue_reassessment(conn, evidence)


def handle_request(store: Store, config: Config, request: dict) -> dict:
    """Return one JSON-compatible response, including stable codes for invalid requests."""
    op = request.get("op") if isinstance(request, dict) else None
    response = {"dreaming_api": 1, "op": op}
    try:
        if (
            not isinstance(request, dict)
            or type(request.get("dreaming_api")) is not int
            or request["dreaming_api"] != 1
        ):
            raise APIError("incompatible_version", "Expected dreaming_api version 1")
        if op in {"knowledge.recall", "knowledge.search"}:
            result = _retrieve(store, config, request)
        elif op == "knowledge.get":
            result = _get(store, request)
        elif op == "knowledge.feedback":
            result = _feedback(store, request)
        elif op == "knowledge.export":
            result = {"export": export_knowledge(store)}
        elif op == "knowledge.import":
            result = import_knowledge(store, request.get("export"))
        else:
            raise APIError("unknown_operation", f"Unknown operation {op}")
        return {**response, "ok": True, **result}
    except APIError as exc:
        return {**response, "ok": False, "code": exc.code, "message": str(exc)}
    except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
        code = "invalid_scope" if str(exc) == "invalid_scope" else "invalid_request"
        if str(exc) == "incompatible_version":
            code = "incompatible_version"
        elif str(exc) == "unknown_learning_recommendation":
            code = "not_found"
        return {**response, "ok": False, "code": code, "message": str(exc)}

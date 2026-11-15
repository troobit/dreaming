"""Portable retained knowledge, without transcripts or runnable run snapshots.

Imported provenance is internal: exchanging it again yields the same document.
Identifiers already present are retained; divergent stores are not reconciled.
"""

import json

from dreaming.knowledge import _rows, blocked_call_refs, normalise_scope
from dreaming.observe import canonical
from dreaming.redact import redact

# Only these tables cross the boundary. Runs carry provenance, never live workers,
# snapshots, selection, credentials, model output or machine-specific document paths.
TABLES = {
    "runs": ("id",),
    "sessions_seen": ("source", "session_id"),
    "patterns": ("id",),
    "registrations": ("tool", "run_ref", "source", "session_id"),
    "observations": ("id",),
    "file_findings": ("id",),
    "research_findings": ("id",),
    "knowledge_items": ("id",),
    "knowledge_revisions": ("item_id", "version"),
    "feedback": ("id",),
    "learning": ("id",),
    "currency_findings": ("id",),
}
RUN_COLUMNS = {"id", "requester", "kind", "revision", "started_at", "ended_at", "status"}
JSON_FIELDS = {
    "sessions_seen": {"lens_versions": dict},
    "patterns": {"lens_scope": dict},
    "registrations": {"user_quote_entries": list},
    "observations": {"outcome_evidence": list, "declared_scope": dict},
    "research_findings": {"sources": list, "evidence_ids": list},
    "knowledge_items": {"scope": dict, "supporting_evidence": list, "contradicting_evidence": list},
    "knowledge_revisions": {
        "scope": dict,
        "supporting_evidence": list,
        "contradicting_evidence": list,
    },
    "feedback": {"revisions": list, "scope": dict},
    "learning": {"scope": dict, "evidence": list},
    "currency_findings": {"sources": list},
}


def _validate_json(field, value):
    if field in {"scope", "declared_scope"} and value is not None:
        normalise_scope(value)
    if field == "sources":
        text_fields = {
            "title",
            "url",
            "link",
            "retrieved_at",
            "claim",
            "passage",
            "text",
            "repo",
            "revision",
            "path",
            "location",
        }
        if value is None or any(
            not isinstance(source, dict)
            or any(not isinstance(source[key], str) for key in source.keys() & text_fields)
            for source in value
        ):
            raise ValueError("invalid_export")
    if field == "revisions" and (
        value is None
        or any(
            not isinstance(ref, dict)
            or not isinstance(ref.get("item_id"), str)
            or type(ref.get("version")) is not int
            or ref["version"] < 1
            or ref.get("usage") not in {"cited", "present_only"}
            for ref in value
        )
    ):
        raise ValueError("invalid_export")


def _columns(conn, table):
    return {
        row[1]: row[2]
        for row in conn.execute(f"PRAGMA table_info({table})")
        if row[1] not in {"imported", "document_path"}
        and (table != "runs" or row[1] in RUN_COLUMNS)
    }


def export_knowledge(store):
    """Read a consistent, deterministic export containing accepted redacted data."""
    result = {"kind": "dreaming-export", "schema_version": 1}
    with store.transaction(write=False) as conn:
        blocked = blocked_call_refs(store)
        for table, keys in TABLES.items():
            columns = _columns(conn, table)
            rows = _rows(
                conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {','.join(keys)}")
            )
            for row in rows:
                if table == "runs":
                    row["status"] = "complete"
                elif table == "sessions_seen" and (
                    row["session_id"] in blocked or row["thread_id"] in blocked
                ):
                    row["forgotten"] = 1
                for name in JSON_FIELDS.get(table, {}):
                    if row.get(name) is not None:
                        row[name] = json.loads(row[name])
            result[table] = rows
    return result


def import_knowledge(store, document):
    """Import once by identity, atomically, with internal imported-evidence markers."""
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("incompatible_version")
    if document.get("kind") != "dreaming-export" or document.keys() - {
        "kind",
        "schema_version",
        *TABLES,
    }:
        raise ValueError("invalid_export")
    rows_by_table = {}
    for table in TABLES:
        rows = document.get(table, [])
        if not isinstance(rows, list):
            raise ValueError("invalid_export")
        columns = _columns(store.connection, table)
        checked = []
        for source_row in rows:
            if not isinstance(source_row, dict) or source_row.keys() - columns.keys():
                raise ValueError("invalid_export")
            row = dict(source_row)
            if any(key not in row for key in TABLES[table]):
                raise ValueError("invalid_export")
            for field, value in row.items():
                if field in JSON_FIELDS.get(table, {}):
                    expected = JSON_FIELDS[table][field]
                    if value is not None and not isinstance(value, expected):
                        raise ValueError("invalid_export")
                    if field in {
                        "supporting_evidence",
                        "contradicting_evidence",
                        "evidence",
                        "outcome_evidence",
                        "user_quote_entries",
                        "evidence_ids",
                    } and (value is None or any(not isinstance(v, str) for v in value)):
                        raise ValueError("invalid_export")
                    _validate_json(field, value)
                    row[field] = redact(canonical(value)) if value is not None else None
                elif value is not None:
                    if columns[field] == "TEXT" and not isinstance(value, str):
                        raise ValueError("invalid_export")
                    if columns[field] == "INTEGER" and type(value) is not int:
                        raise ValueError("invalid_export")
            # Never trust the wire document to describe a resumable process.
            if table == "runs":
                row["status"] = "complete"
            if table in {"observations", "file_findings", "knowledge_items"}:
                row["imported"] = 1
            # Scrub externally supplied content before it reaches persistent storage.
            for field in {
                "redacted_quote",
                "finding",
                "summary",
                "confidence_basis",
                "correction",
                "reason",
                "next_step",
                "claim",
            } & row.keys():
                if row[field] is not None:
                    row[field] = redact(row[field])
            checked.append(row)
        rows_by_table[table] = checked
    before = store.connection.total_changes
    with store.transaction() as conn:
        # Patterns may refer forward to a merged pattern, and feedback/revisions
        # can refer to runs/items appearing later in the same exchange.
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for table, keys in TABLES.items():
            for row in rows_by_table[table]:
                conn.execute(
                    f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)}) "
                    f"ON CONFLICT({','.join(keys)}) DO NOTHING",
                    tuple(row.values()),
                )
    return {"changed": store.connection.total_changes > before}

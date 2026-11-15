"""Exchange: retained knowledge, redacted evidence, history and feedback as one
versioned JSON document, and its import into another store (14.3).

The document is `{"kind": "dreaming-export", "schema_version": 1, "tables":
{...}}`, each table a list of rows by column name in primary-key order, with
JSON columns as values. An import adds the rows whose key the store does not
have and leaves the others as they are, so importing the same export again
changes nothing; it is not a merge of diverging stores (non-goal).

Imported runs, evidence, items and learning are marked `imported`: the evidence
never counts toward this store's promotion thresholds, and the runs are lineage,
not this store's dreams. The marker is not exported, so a store built by import
exports the same document. Forgotten sessions keep their flag, so the exclusion
holds in the new store (2.11), and each exported observation says whether its
session is forgotten.

An export waits until no dream is unfinished, because an unfinished dream's
revisions are not yet retained.
"""

from __future__ import annotations

import json
import sqlite3

from dreaming import store
from dreaming.errors import FAILURE, USAGE_ERROR, DreamingError

KIND = "dreaming-export"
SCHEMA_VERSION = 1

# Tables in foreign-key order, each with its primary key and JSON columns.
TABLES = {
    "runs": (("id",), ()),
    "patterns": (("id",), ("scope",)),
    "sessions_seen": (("source", "session_id"), ("lens_versions", "inventory")),
    "observations": (("evidence_id",), ()),
    "file_findings": (("evidence_id",), ()),
    "research_findings": (("id",), ("sources",)),
    "knowledge_items": (("id",), ("scope",)),
    "knowledge_revisions": (
        ("item_id", "version"),
        ("scope", "supporting", "contradicting"),
    ),
    "feedback": (("id",), ("revisions", "correction")),
    "learning": (("id",), ("scope", "evidence", "research")),
}
# Of a run, only what it was and when; its configuration, snapshot and process
# stay with the store that ran it.
RUN_COLUMNS = (
    "id",
    "kind",
    "requester",
    "status",
    "revision",
    "started_at",
    "ended_at",
)
MARKER = "imported"


def _invalid(message: str, code: str = "invalid_request") -> DreamingError:
    return DreamingError(message, USAGE_ERROR, code)


def _names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _columns(names: list[str], table: str) -> list[str]:
    """The columns exported: every one but the store-local marker, and only a
    run's lineage."""
    if table == "runs":
        return [name for name in RUN_COLUMNS if name in names]
    return [name for name in names if name != MARKER]


def export(conn: sqlite3.Connection) -> dict:
    """The store's retained knowledge as one document."""
    row = conn.execute(
        f"SELECT id, status FROM runs WHERE status IN {store.UNFINISHED_RUN_STATUSES}"
        " ORDER BY started_at LIMIT 1"
    ).fetchone()
    if row is not None:
        raise DreamingError(
            f"dream {row['id']} is {row['status']} and its revisions are not "
            "retained yet; export after it ends, or resume or discard it",
            FAILURE,
            "run_unfinished",
        )
    tables = {}
    for table, (key, json_columns) in TABLES.items():
        columns = _columns(_names(conn, table), table)
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(key)}"
        )
        tables[table] = [
            {
                name: json.loads(value)
                if name in json_columns and value is not None
                else value
                for name, value in zip(columns, row, strict=True)
            }
            for row in rows
        ]
    forgotten = {
        (s["source"], s["session_id"])
        for s in tables["sessions_seen"]
        if s["forgotten"]
    }
    for observation in tables["observations"]:
        observation["forgotten"] = (
            observation["source"],
            observation["session_id"],
        ) in forgotten
    return {"kind": KIND, "schema_version": SCHEMA_VERSION, "tables": tables}


def import_(conn: sqlite3.Connection, document: object) -> dict:
    """Add an export's rows to the store in one transaction, and count the rows
    added per table."""
    if not isinstance(document, dict) or document.get("kind") != KIND:
        raise _invalid(f"an import takes a {KIND} document")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid("the export names no schema_version")
    if version != SCHEMA_VERSION:
        raise _invalid(
            f"the export's schema version {version} is not {SCHEMA_VERSION}",
            "incompatible_version",
        )
    tables = document.get("tables")
    if not isinstance(tables, dict) or not all(
        isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        for rows in tables.values()
    ):
        raise _invalid("the export's tables must map names to lists of rows")
    added: dict[str, int] = {}
    try:
        with store.transaction(conn):
            for table, (key, json_columns) in TABLES.items():
                names = _names(conn, table)
                known = set(_columns(names, table))
                added[table] = 0
                for row in tables.get(table, []):
                    values = {
                        name: json.dumps(value)
                        if name in json_columns and value is not None
                        else value
                        for name, value in row.items()
                        if name in known
                    }
                    match = " AND ".join(f"{name} = :{name}" for name in key)
                    if conn.execute(
                        f"SELECT 1 FROM {table} WHERE {match}", values
                    ).fetchone():
                        continue
                    if MARKER in names:
                        values[MARKER] = 1
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(values)})"
                        f" VALUES ({', '.join(':' + name for name in values)})",
                        values,
                    )
                    added[table] += 1
    except sqlite3.Error as exc:
        raise _invalid(
            f"the export does not fit this store: {exc}", "invalid_export"
        ) from exc
    return {"imported": added}

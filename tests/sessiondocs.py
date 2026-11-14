"""Synthetic sessions, store rows and a stub apsis client for tests."""

import copy
import json
from datetime import UTC, datetime, timedelta

from apsis_contract import FIXTURES

from dreaming import sessions
from dreaming.errors import DreamingError

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days: float) -> str:
    return iso(NOW - timedelta(days=days))


def make_entry(role, text, when=None, **extra):
    data = {"role": role, "content": [{"type": "text", "text": text}], **extra}
    if when:
        data["timestamp"] = when
    return data


def make_doc(source, sid, created, *, entries=None, **meta):
    meta = {"source": source, "id": sid, "created_at": created, **meta}
    meta.setdefault("project", "/work/repo")
    if entries is None:
        entries = [
            make_entry("user", f"question in {sid}", created, id=f"{sid}-u1"),
            make_entry("assistant", f"answer in {sid}", created, id=f"{sid}-a1"),
        ]
    return {
        "schema_version": 1,
        "kind": "session",
        "session": {key: value for key, value in meta.items() if value is not None},
        "entries": entries,
        "warnings": [],
    }


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class StubApsis:
    """Serves parsed shared-json documents; the listing is derived from them."""

    def __init__(self, *docs, failing=(), listing_warnings=()):
        self.docs = {(d["session"]["source"], d["session"]["id"]): d for d in docs}
        self.failing = dict(failing)
        self.listing_warnings = list(listing_warnings)
        self.opened = []

    def list_sessions(self):
        keys = (
            "source",
            "id",
            "project",
            "created_at",
            "file_modified_at",
            "model",
            "provider",
            "origin",
            "thread_id",
            "size_bytes",
        )
        rows = [
            {key: doc["session"][key] for key in keys if key in doc["session"]}
            for doc in self.docs.values()
        ]
        # Failing sessions are listed as the newest, so they are opened first.
        rows += [
            {"source": s, "id": i, "created_at": days_ago(1)} for (s, i) in self.failing
        ]
        return sessions.parse_listing(
            {
                "schema_version": 1,
                "kind": "listing",
                "scope": {"all_projects": True},
                "sessions": rows,
                "warnings": self.listing_warnings,
            }
        )

    def session(self, source, session_id):
        self.opened.append((source, session_id))
        if (source, session_id) in self.failing:
            raise DreamingError(self.failing[(source, session_id)], 1, "apsis_failed")
        return sessions.parse_session(copy.deepcopy(self.docs[(source, session_id)]))


def select(db, apsis, cfg, home, request=None, **kwargs):
    return sessions.select_sessions(
        db,
        apsis,
        cfg,
        request or sessions.SelectionRequest(),
        home=home,
        now=NOW,
        **kwargs,
    )


def selected_ids(selection):
    return [s.id for s in selection.sessions]


def add_run(db, run_id):
    db.execute(
        "INSERT INTO runs (id, status, revision, started_at) VALUES (?, ?, ?, ?)",
        (run_id, "running", "abc", iso(NOW)),
    )


def add_seen(db, source, sid, *, state="read", entries_read=2, thread_id=None):
    db.execute(
        "INSERT INTO sessions_seen (source, session_id, thread_id, state, entries_read)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, sid, thread_id, state, entries_read),
    )


def add_pattern(db, pattern_id):
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at) VALUES (?, ?, 'prefs', ?)",
        (pattern_id, pattern_id, iso(NOW)),
    )


def add_observation(
    db,
    evidence_id,
    source,
    session_id,
    *,
    entry_id=None,
    kind="user_statement",
    modality="asserted",
    outcome="unknown",
    pattern_id=None,
    project=None,
    observed_on="2026-11-01",
    domain=None,
    lens="prefs",
    subject="user",
):
    entry_id = entry_id or evidence_id
    db.execute(
        "INSERT INTO observations (evidence_id, source, session_id, entry_id,"
        " location, representation_version, quote, kind, modality, outcome, lens,"
        " lens_version, pattern_id, project, observed_on, domain, subject)"
        " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            source,
            session_id,
            entry_id,
            entry_id,
            f"quote {evidence_id}",
            kind,
            modality,
            outcome,
            lens,
            pattern_id,
            project,
            observed_on,
            domain,
            subject,
        ),
    )


def add_item(db, item_id, supporting, *, kind="preference", contradicting=()):
    db.execute(
        "INSERT INTO knowledge_items (id, kind, status, created_at, updated_at)"
        " VALUES (?, ?, 'active', ?, ?)",
        (item_id, kind, iso(NOW), iso(NOW)),
    )
    db.execute(
        "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
        " confidence, supporting, contradicting, actor, created_at)"
        " VALUES (?, 1, ?, ?, 'active', 'medium', ?, ?, 'dream', ?)",
        (
            item_id,
            f"content {item_id}",
            kind,
            json.dumps(list(supporting)),
            json.dumps(list(contradicting)),
            iso(NOW),
        ),
    )


def pending(db):
    rows = db.execute("SELECT id, pending_reassessment FROM knowledge_items")
    return {row["id"]: row["pending_reassessment"] for row in rows}

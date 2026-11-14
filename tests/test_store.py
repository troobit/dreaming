"""The SQLite store: schema, WAL and additive migrations (6.x, 7.5, 13.x)."""

import sqlite3
import threading
import time

import pytest

from dreaming import store
from dreaming.errors import DreamingError

TABLES = {
    "runs",
    "steps",
    "sessions_seen",
    "calls",
    "observations",
    "file_findings",
    "research_findings",
    "patterns",
    "knowledge_items",
    "knowledge_revisions",
    "feedback",
    "changes",
    "adoption",
    "registrations",
    "learning",
    "replays",
    "currency_findings",
}


@pytest.fixture
def db(tmp_path):
    conn = store.open_store(tmp_path / "dreaming.db")
    yield conn
    conn.close()


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert_run(conn, run_id="r1", status="running"):
    conn.execute(
        "INSERT INTO runs (id, status, revision, started_at) VALUES (?, ?, ?, ?)",
        (run_id, status, "abc123", "2026-11-14T00:00:00Z"),
    )


def test_every_table_in_the_data_model_exists(db):
    names = {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert TABLES <= names


def test_fresh_store_is_at_the_latest_version(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(store.MIGRATIONS)


def test_runs_accept_the_waiting_status_and_record_a_wait_until_time(db):
    assert {"status", "wait_until", "requester", "revision"} <= _columns(db, "runs")
    for status in store.RUN_STATUSES:
        _insert_run(db, f"run-{status}", status)
    assert "waiting" in store.RUN_STATUSES
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(db, "bad", "sleeping")


def test_evidence_tables_carry_source_availability_markers(db):
    for table in ("observations", "file_findings", "sessions_seen"):
        assert "availability" in _columns(db, table), table


def test_store_uses_wal_with_a_busy_timeout(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_reopening_is_a_no_op(tmp_path):
    path = tmp_path / "dreaming.db"
    store.open_store(path).close()
    conn = store.open_store(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    conn.close()


def test_every_migration_statement_is_additive():
    for number, script in enumerate(store.MIGRATIONS, start=1):
        for statement in store.statements(script):
            words = statement.upper().split()
            additive = words[:2] in (["CREATE", "TABLE"], ["CREATE", "INDEX"]) or (
                words[:2] == ["ALTER", "TABLE"] and words[3:5] == ["ADD", "COLUMN"]
            )
            assert additive, f"migration {number}: {statement}"


def test_an_added_migration_keeps_existing_data(tmp_path):
    path = tmp_path / "dreaming.db"
    conn = store.open_store(path)
    _insert_run(conn, "r1", "complete")
    conn.close()

    extra = [*store.MIGRATIONS, "ALTER TABLE runs ADD COLUMN note TEXT;"]
    conn = store.open_store(path, migrations=extra)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(extra)
    row = conn.execute("SELECT id, status, note FROM runs").fetchone()
    assert (row["id"], row["status"], row["note"]) == ("r1", "complete", None)
    conn.close()


@pytest.mark.parametrize("status", ["running", "waiting", "interrupted"])
def test_migration_is_blocked_while_a_run_is_unfinished(tmp_path, status):
    path = tmp_path / "dreaming.db"
    conn = store.open_store(path)
    _insert_run(conn, "dream-42", status)
    conn.close()

    extra = [*store.MIGRATIONS, "ALTER TABLE runs ADD COLUMN note TEXT;"]
    with pytest.raises(DreamingError) as excinfo:
        store.open_store(path, migrations=extra)
    assert excinfo.value.code == "migration_blocked"
    assert "dream-42" in excinfo.value.message

    conn = store.open_store(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    assert "note" not in _columns(conn, "runs")
    conn.close()


def test_a_store_newer_than_the_program_is_refused(tmp_path):
    path = tmp_path / "dreaming.db"
    extra = [*store.MIGRATIONS, "ALTER TABLE runs ADD COLUMN note TEXT;"]
    store.open_store(path, migrations=extra).close()
    with pytest.raises(DreamingError) as excinfo:
        store.open_store(path)
    assert excinfo.value.code == "incompatible_version"


def test_a_failed_migration_leaves_the_store_unchanged(tmp_path):
    path = tmp_path / "dreaming.db"
    store.open_store(path).close()
    broken = [
        *store.MIGRATIONS,
        "ALTER TABLE runs ADD COLUMN note TEXT; ALTER TABLE missing ADD COLUMN x TEXT;",
    ]
    with pytest.raises(sqlite3.OperationalError):
        store.open_store(path, migrations=broken)
    conn = store.open_store(path)
    assert "note" not in _columns(conn, "runs")
    conn.close()


def test_a_second_connection_can_write_while_a_run_holds_a_transaction(tmp_path):
    path = tmp_path / "dreaming.db"
    run_conn = store.open_store(path)
    reader = store.open_store(path)
    _insert_run(run_conn)

    run_conn.execute("BEGIN IMMEDIATE")
    run_conn.execute("UPDATE runs SET status = 'waiting' WHERE id = 'r1'")

    # Readers are never blocked by the writer under WAL.
    assert reader.execute("SELECT status FROM runs").fetchone()[0] == "running"

    errors = []

    def write_feedback():
        api_conn = store.open_store(path)
        try:
            with store.transaction(api_conn):
                api_conn.execute(
                    "INSERT INTO feedback (id, revisions, basis, received_at) "
                    "VALUES ('fb1', '[]', 'unknown', '2026-11-14T00:00:00Z')"
                )
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            api_conn.close()

    writer = threading.Thread(target=write_feedback)
    writer.start()
    time.sleep(0.2)
    run_conn.execute("COMMIT")
    writer.join(timeout=10)

    assert errors == []
    assert reader.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
    run_conn.close()
    reader.close()


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError), store.transaction(db):
        _insert_run(db)
        raise RuntimeError("boom")
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_the_same_observation_is_stored_once(db):
    _insert_run(db)
    row = {
        "run": "r1",
        "source": "claude",
        "session_id": "s1",
        "entry_id": "e1",
        "location": "e1",
        "representation_version": 1,
        "quote": "use tabs",
        "kind": "user_statement",
        "modality": "asserted",
        "lens": "preferences",
        "lens_version": 1,
    }
    sql = (
        "INSERT OR IGNORE INTO observations (evidence_id, run, source, session_id, "
        "entry_id, location, representation_version, quote, kind, modality, lens, "
        "lens_version) VALUES (:evidence_id, :run, :source, :session_id, :entry_id, "
        ":location, :representation_version, :quote, :kind, :modality, :lens, "
        ":lens_version)"
    )
    db.execute(sql, {**row, "evidence_id": "ob-1"})
    db.execute(sql, {**row, "evidence_id": "ob-2"})
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1

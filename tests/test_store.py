import sqlite3
import stat

import pytest

from dreaming.config import ConfigError
from dreaming.store import MIGRATIONS, Migration, MigrationBlocked, Store, StoreError

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


def test_schema_and_private_wal_store(tmp_path):
    with Store(tmp_path / "private") as store:
        conn = store.connection
        assert TABLES <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        conn.execute("INSERT INTO runs(id, status, wait_until) VALUES ('r', 'waiting', 'tomorrow')")
        assert conn.execute("SELECT wait_until FROM runs").fetchone()[0] == "tomorrow"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO runs(id, status) VALUES ('bad', 'nonsense')")


@pytest.mark.parametrize("state", ["running", "waiting", "interrupted", "partial"])
def test_migration_blocked_names_unfinished_run(tmp_path, state):
    home = tmp_path / "private"
    with Store(home) as store:
        store.connection.execute("INSERT INTO runs(id, status) VALUES ('unfinished', ?)", (state,))
    extra = (*MIGRATIONS, Migration(2, "ALTER TABLE runs ADD COLUMN extra TEXT;"))
    with pytest.raises(MigrationBlocked, match="migration_blocked.*unfinished"):
        Store(home, migrations=extra)
    with Store(home) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 1
        store.connection.execute("UPDATE runs SET status='complete'")
    with Store(home, migrations=extra) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.connection.execute("SELECT id, extra FROM runs").fetchone() == (
            "unfinished",
            None,
        )


def test_failed_migration_is_atomic(tmp_path):
    home = tmp_path / "private"
    with Store(home):
        pass
    broken = (
        *MIGRATIONS,
        Migration(
            2, "ALTER TABLE runs ADD COLUMN extra TEXT;\nCREATE TABLE runs (duplicate TEXT);"
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        Store(home, migrations=broken)
    with Store(home) as store:
        assert "extra" not in {r[1] for r in store.connection.execute("PRAGMA table_info(runs)")}
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_destructive_migration_refused(tmp_path):
    with pytest.raises(StoreError, match="additive"):
        Store(tmp_path / "private", migrations=(*MIGRATIONS, Migration(2, "DROP TABLE runs;")))


def test_newer_database_refused(tmp_path):
    home = tmp_path / "private"
    with Store(home) as store:
        store.connection.execute("PRAGMA user_version=99")
    with pytest.raises(StoreError, match="newer"):
        Store(home)


def test_other_connection_writes_during_read_transaction(tmp_path):
    home = tmp_path / "private"
    with Store(home) as first, Store(home) as second:
        with first.transaction(write=False) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            second.connection.execute("INSERT INTO runs(id, status) VALUES ('r', 'running')")
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert first.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_step_result_and_followup_share_rollback(tmp_path):
    with Store(tmp_path / "private") as store:
        store.connection.execute("INSERT INTO runs(id, status) VALUES ('r', 'running')")
        with pytest.raises(RuntimeError), store.transaction() as conn:
            conn.execute("INSERT INTO steps(run_id, key, phase) VALUES ('r', 'read:1', 'Read')")
            raise RuntimeError("crash")
        assert store.connection.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0


def test_database_symlink_cannot_write_into_repository(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    home = tmp_path / "private"
    home.mkdir()
    target = repo / "database.db"
    (home / "dreaming.db").symlink_to(target)
    with pytest.raises(ConfigError):
        Store(home)
    assert not target.exists()

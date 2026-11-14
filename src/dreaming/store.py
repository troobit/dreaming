"""Private SQLite storage with short explicit transactions and additive migrations."""

import re
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from dreaming.config import ensure_home, require_external


class StoreError(RuntimeError):
    pass


class MigrationBlocked(StoreError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str


MIGRATIONS = (
    Migration(1, files("dreaming").joinpath("schema.sql").read_text()),
    Migration(2, files("dreaming").joinpath("session_migration.sql").read_text()),
    Migration(3, files("dreaming").joinpath("observation_migration.sql").read_text()),
)
UNFINISHED = ("running", "waiting", "interrupted", "partial")


def _statements(sql: str) -> Iterator[str]:
    statement = ""
    for char in sql:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            yield statement.strip()
            statement = ""
    if statement.strip():
        raise StoreError("Migration SQL must end each statement with a semicolon")


def snapshot_connection(path: Path) -> sqlite3.Connection:
    """Read a stable DB/WAL copy without letting SQLite create or update live sidecars."""
    paths = (path, Path(str(path) + "-wal"))

    def stamps():
        result = []
        for p in paths:
            try:
                stat = p.stat()
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                result.append(None)
        return result

    for _ in range(5):
        before = stamps()
        try:
            contents = [
                p.read_bytes() if stamp else None for p, stamp in zip(paths, before, strict=True)
            ]
        except FileNotFoundError:
            continue
        if before == stamps():
            break
    else:
        raise StoreError("Database is changing; retry inspection")
    with tempfile.TemporaryDirectory(prefix="dreaming-inspect-") as directory:
        copy = Path(directory) / "dreaming.db"
        for suffix, content in zip(("", "-wal"), contents, strict=True):
            if content is not None:
                Path(str(copy) + suffix).write_bytes(content)
        memory = sqlite3.connect(":memory:", isolation_level=None)
        try:
            source = sqlite3.connect(copy)
            try:
                source.backup(memory)
            finally:
                source.close()
        except BaseException:
            memory.close()
            raise
    memory.execute("PRAGMA query_only=ON")
    return memory


class Store:
    @classmethod
    def readonly(cls, home: Path) -> "Store":
        """Inspect without creating a home or migrating an existing database."""
        store = cls.__new__(cls)
        store.path = require_external(home / "dreaming.db")
        if store.path.exists():
            store.connection = snapshot_connection(store.path)
            current = store.connection.execute("PRAGMA user_version").fetchone()[0]
            if current != len(MIGRATIONS):
                store.close()
                raise StoreError("Database needs a compatible schema before read-only planning")
        else:
            store.connection = sqlite3.connect(":memory:", isolation_level=None)
            for migration in MIGRATIONS:
                for statement in _statements(migration.sql):
                    store.connection.execute(statement)
        store.connection.execute("PRAGMA query_only=ON")
        return store

    def __init__(self, home: Path | None = None, *, migrations: Sequence[Migration] = MIGRATIONS):
        self.path = require_external(ensure_home(home) / "dreaming.db")
        # Create privately before SQLite opens it; the home also protects WAL/SHM files.
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self._migrate(migrations)
        except BaseException:
            self.connection.close()
            raise

    def _migrate(self, migrations: Sequence[Migration]) -> None:
        if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
            raise StoreError("Migration versions must be consecutive from 1")
        with self.transaction() as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(migrations):
                raise StoreError("Database schema is newer than this program")
            if current == len(migrations):
                return
            if current:
                unfinished = conn.execute(
                    "SELECT id FROM runs WHERE status IN (?,?,?,?) ORDER BY id LIMIT 1", UNFINISHED
                ).fetchone()
                if unfinished:
                    raise MigrationBlocked(
                        f"migration_blocked: resume or discard run {unfinished[0]}"
                    )
            for migration in migrations[current:]:
                for statement in _statements(migration.sql):
                    if not re.match(
                        r"(?is)^(CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s|"
                        r"ALTER\s+TABLE\s+\w+\s+ADD\s+(?:COLUMN\s+)?\w+\s)",
                        statement,
                    ):
                        raise StoreError("Migrations must be additive (create or add column)")
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={migration.version}")

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Never hold a write transaction across a subprocess or model call."""
        self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

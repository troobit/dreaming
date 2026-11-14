"""Private SQLite storage with short explicit transactions and additive migrations."""

import re
import sqlite3
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


MIGRATIONS = (Migration(1, files("dreaming").joinpath("schema.sql").read_text()),)
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


class Store:
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

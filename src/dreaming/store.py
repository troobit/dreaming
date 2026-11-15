"""The SQLite store at `~/.dreaming/dreaming.db`.

Migrations are numbered by `PRAGMA user_version` and are additive: a migration
only creates tables or indexes or adds columns, so an older program version can
still read a newer store within the same major version. A migration runs only
when no run is unfinished, because an interrupted run resumes on the program
version it started with (7.3, Decision 11).

Evidence is stored redacted; every source reference carries an availability
marker (`available|unavailable|changed`, 13.7).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from dreaming.errors import FAILURE, DreamingError

BUSY_TIMEOUT_SECONDS = 30.0
STORE_NAME = "dreaming.db"

RUN_STATUSES = (
    "running",
    "waiting",
    "interrupted",
    "complete",
    "partial",
    "budget_exhausted",
    "discarded",
)
UNFINISHED_RUN_STATUSES = ("running", "waiting", "interrupted")

_AVAILABILITY = (
    "availability TEXT NOT NULL DEFAULT 'available' "
    "CHECK (availability IN ('available', 'unavailable', 'changed'))"
)

_SCHEMA_V1 = f"""
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'dream' CHECK (kind IN ('dream', 'currency')),
    requester TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL CHECK (status IN {RUN_STATUSES}),
    revision TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    process_group INTEGER,
    selection TEXT NOT NULL DEFAULT '{{}}',
    overrides TEXT NOT NULL DEFAULT '{{}}',
    budget TEXT NOT NULL DEFAULT '{{}}',
    snapshot_path TEXT,
    wait_until TEXT,
    wait_notice TEXT,
    report_path TEXT
);

CREATE TABLE steps (
    run TEXT NOT NULL REFERENCES runs (id),
    key TEXT NOT NULL,
    phase TEXT NOT NULL
        CHECK (phase IN ('read', 'review', 'research', 'author', 'adopt')),
    depends_on TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'running', 'done', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    input_ref TEXT,
    reason TEXT,
    PRIMARY KEY (run, key)
);

CREATE TABLE sessions_seen (
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    harness TEXT,
    model TEXT,
    provider TEXT,
    locality TEXT NOT NULL DEFAULT 'unknown'
        CHECK (locality IN ('local', 'cloud', 'unknown')),
    project TEXT,
    domain TEXT,
    workflow TEXT,
    origin TEXT,
    interactive INTEGER,
    subject TEXT NOT NULL DEFAULT 'user' CHECK (subject IN ('user', 'dreaming')),
    started_at TEXT,
    lens_versions TEXT NOT NULL DEFAULT '{{}}',
    entries_read INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('read', 'skipped', 'failed')),
    reason TEXT,
    forgotten INTEGER NOT NULL DEFAULT 0,
    {_AVAILABILITY},
    last_run TEXT REFERENCES runs (id),
    PRIMARY KEY (source, session_id)
);
CREATE INDEX sessions_seen_thread ON sessions_seen (thread_id);

CREATE TABLE calls (
    id INTEGER PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    step_key TEXT,
    attempt INTEGER NOT NULL,
    role TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT,
    harness_session_id TEXT,
    session_ref TEXT,
    input_ref TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    usage_estimated INTEGER NOT NULL DEFAULT 0,
    seconds REAL,
    raw_output TEXT,
    outcome TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX calls_run_step ON calls (run, step_key);

CREATE TABLE patterns (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    lens TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    created_run TEXT REFERENCES runs (id),
    last_reviewed_run TEXT REFERENCES runs (id)
);

CREATE TABLE observations (
    evidence_id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    harness TEXT,
    entry_id TEXT NOT NULL,
    location TEXT NOT NULL,
    representation_version INTEGER NOT NULL,
    quote TEXT NOT NULL,
    kind TEXT NOT NULL,
    modality TEXT NOT NULL CHECK (modality IN ('asserted', 'hedged', 'tentative')),
    outcome TEXT NOT NULL DEFAULT 'unknown'
        CHECK (outcome IN ('user_confirmed', 'check_confirmed', 'failed', 'unknown')),
    outcome_evidence TEXT,
    lens TEXT NOT NULL,
    lens_version INTEGER NOT NULL,
    pattern_key TEXT,
    pattern_id TEXT REFERENCES patterns (id),
    project TEXT,
    domain TEXT,
    observed_on TEXT,
    backend TEXT,
    model TEXT,
    subject TEXT NOT NULL DEFAULT 'user' CHECK (subject IN ('user', 'dreaming')),
    excluded INTEGER NOT NULL DEFAULT 0,
    {_AVAILABILITY},
    UNIQUE (source, session_id, entry_id, lens, lens_version, quote)
);
CREATE INDEX observations_pattern ON observations (pattern_id);

CREATE TABLE file_findings (
    evidence_id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    repo TEXT NOT NULL,
    revision TEXT NOT NULL,
    path TEXT NOT NULL,
    location TEXT NOT NULL,
    finding TEXT NOT NULL,
    lens TEXT NOT NULL,
    lens_version INTEGER NOT NULL,
    pattern_id TEXT REFERENCES patterns (id),
    project TEXT,
    domain TEXT,
    found_on TEXT,
    backend TEXT,
    model TEXT,
    {_AVAILABILITY},
    UNIQUE (repo, revision, path, location, lens, lens_version, finding)
);

CREATE TABLE research_findings (
    id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    question TEXT NOT NULL,
    finding TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    fetched INTEGER NOT NULL DEFAULT 0,
    backend TEXT,
    model TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE knowledge_items (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN (
            'candidate', 'active', 'contested', 'superseded', 'dismissed'
        )),
    scope TEXT NOT NULL DEFAULT '{{}}',
    summary TEXT NOT NULL DEFAULT '',
    pattern_id TEXT REFERENCES patterns (id),
    pending_reassessment INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_revisions (
    item_id TEXT NOT NULL REFERENCES knowledge_items (id),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{{}}',
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    confidence_basis TEXT NOT NULL DEFAULT '',
    supporting TEXT NOT NULL DEFAULT '[]',
    contradicting TEXT NOT NULL DEFAULT '[]',
    latest_support_date TEXT,
    lens TEXT,
    lens_version INTEGER,
    backend TEXT,
    model TEXT,
    originating_run TEXT REFERENCES runs (id),
    feedback_id TEXT,
    actor TEXT NOT NULL CHECK (actor IN ('dream', 'user', 'feedback', 'import')),
    base_version INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, version)
);

CREATE TABLE feedback (
    id TEXT PRIMARY KEY,
    revisions TEXT NOT NULL DEFAULT '[]',
    run_ref TEXT,
    session_ref TEXT,
    outcome TEXT,
    basis TEXT NOT NULL
        CHECK (basis IN ('user_confirmed', 'check_confirmed', 'unknown')),
    check_name TEXT,
    correction TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE changes (
    id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    pattern_id TEXT REFERENCES patterns (id),
    currency_finding_id TEXT,
    repo TEXT NOT NULL,
    branch TEXT,
    commit_sha TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    external_input INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'applied', 'waiting', 'withheld', 'rejected', 'failed')
    ),
    reason TEXT,
    rank TEXT NOT NULL DEFAULT '{{}}',
    patch_path TEXT,
    applied_at TEXT,
    reverted_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE adoption (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs (id),
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    step TEXT NOT NULL CHECK (step IN (
        'checking', 'checked', 'merging', 'merged', 'deploying', 'deployed', 'reverted'
    )),
    head_before TEXT,
    head_after TEXT,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE TABLE registrations (
    tool TEXT NOT NULL,
    run_ref TEXT NOT NULL,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    correlation_id TEXT,
    workflow TEXT,
    project TEXT,
    user_quote_entries TEXT NOT NULL DEFAULT '[]',
    registered_at TEXT NOT NULL,
    PRIMARY KEY (tool, run_ref, source, session_id)
);

CREATE TABLE learning (
    id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    scope TEXT NOT NULL DEFAULT '{{}}',
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    research TEXT NOT NULL DEFAULT '[]',
    next_step TEXT,
    outcome TEXT,
    disposition TEXT NOT NULL DEFAULT 'open'
        CHECK (disposition IN ('open', 'dismissed', 'deferred', 'addressed')),
    deferred_until TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE replays (
    id INTEGER PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs (id),
    change_id TEXT NOT NULL REFERENCES changes (id),
    source TEXT,
    session_id TEXT,
    backend TEXT,
    model TEXT,
    recurs_without INTEGER,
    judgement TEXT CHECK (judgement IN ('recurs', 'resolved', 'unclear')),
    tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE currency_findings (
    id TEXT PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    target TEXT NOT NULL,
    claim TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL
        CHECK (status IN ('candidate', 'note', 'proposed', 'dismissed')),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# The listing size at the last read, so selection can pass over an unchanged
# processed session without opening it (2.5).
_SCHEMA_V2 = """
ALTER TABLE sessions_seen ADD COLUMN size_bytes INTEGER;
ALTER TABLE sessions_seen ADD COLUMN request_count INTEGER;
"""

# The run engine: step order, wall-clock time spent running (1.2) and why a run
# stopped short.
_SCHEMA_V3 = """
ALTER TABLE steps ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN error TEXT;
"""

# Observations and review: what the program rejected and why (6.2, 7.5, 11.6),
# the reviewer's candidates with their rank (11.3) and research questions, merged
# patterns (11.4), and the pattern a learning recommendation rests on (15.3).
_SCHEMA_V4 = """
CREATE TABLE rejections (
    id INTEGER PRIMARY KEY,
    run TEXT REFERENCES runs (id),
    step_key TEXT,
    what TEXT NOT NULL,
    subject TEXT,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX rejections_run ON rejections (run, what);

CREATE TABLE candidates (
    id TEXT PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs (id),
    pattern_id TEXT REFERENCES patterns (id),
    proposal TEXT NOT NULL CHECK (proposal IN ('change', 'learning')),
    repo TEXT,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    position INTEGER,
    signals TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'authored', 'deferred', 'discarded')),
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE questions (
    id TEXT PRIMARY KEY,
    run TEXT NOT NULL REFERENCES runs (id),
    candidate_id TEXT REFERENCES candidates (id),
    question TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

ALTER TABLE patterns ADD COLUMN merged_into TEXT;
ALTER TABLE learning ADD COLUMN pattern_id TEXT;
"""

# Research and authoring: findings by question with their assessment, sources the
# researcher could not read (4.6), interactive skill steps the author deferred
# (9.3), and an authored change's edits, message, research and base revision.
_SCHEMA_V5 = """
ALTER TABLE research_findings ADD COLUMN question_id TEXT;
ALTER TABLE research_findings ADD COLUMN assessment TEXT;
ALTER TABLE questions ADD COLUMN unavailable TEXT NOT NULL DEFAULT '[]';
ALTER TABLE candidates ADD COLUMN deferred_steps TEXT NOT NULL DEFAULT '[]';
ALTER TABLE changes ADD COLUMN candidate_id TEXT;
ALTER TABLE changes ADD COLUMN summary TEXT;
ALTER TABLE changes ADD COLUMN commit_message TEXT;
ALTER TABLE changes ADD COLUMN edits TEXT NOT NULL DEFAULT '[]';
ALTER TABLE changes ADD COLUMN research TEXT NOT NULL DEFAULT '[]';
ALTER TABLE changes ADD COLUMN base_revision TEXT;
"""

# Exchange (14.3): rows that came from another store's export. Imported evidence
# never counts toward this store's promotion thresholds, and imported runs are
# lineage, not this store's dreams.
_SCHEMA_V6 = """
ALTER TABLE runs ADD COLUMN imported INTEGER NOT NULL DEFAULT 0;
ALTER TABLE observations ADD COLUMN imported INTEGER NOT NULL DEFAULT 0;
ALTER TABLE file_findings ADD COLUMN imported INTEGER NOT NULL DEFAULT 0;
ALTER TABLE learning ADD COLUMN imported INTEGER NOT NULL DEFAULT 0;
"""

MIGRATIONS: list[str] = [
    _SCHEMA_V1,
    _SCHEMA_V2,
    _SCHEMA_V3,
    _SCHEMA_V4,
    _SCHEMA_V5,
    _SCHEMA_V6,
]


def statements(script: str) -> list[str]:
    """Split a migration script into complete statements."""
    result, pending = [], ""
    for piece in script.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            if pending.strip(" \t\n;"):
                result.append(pending.strip())
            pending = ""
    return result


def store_path(home: Path) -> Path:
    return home / STORE_NAME


def connect(path: Path) -> sqlite3.Connection:
    """Open in WAL mode with a busy timeout; callers manage transactions explicitly."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the block in one write transaction, rolled back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations: Sequence[str] = MIGRATIONS) -> None:
    """Bring the store to the latest version, refusing while a run is unfinished."""
    target = len(migrations)
    if _user_version(conn) == target:
        return
    with transaction(conn):
        current = _user_version(conn)
        if current > target:
            raise DreamingError(
                f"the store is at schema version {current}, newer than this "
                f"program's {target}; use a newer dream",
                FAILURE,
                "incompatible_version",
            )
        if current == target:
            return
        if current > 0:
            row = conn.execute(
                "SELECT id, status FROM runs WHERE status IN "
                f"{UNFINISHED_RUN_STATUSES} ORDER BY started_at LIMIT 1"
            ).fetchone()
            if row is not None:
                raise DreamingError(
                    f"store migration is blocked by run {row['id']} ({row['status']}); "
                    "resume or discard it first",
                    FAILURE,
                    "migration_blocked",
                )
        for number in range(current, target):
            for statement in statements(migrations[number]):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {target}")


def memory_copy(path: Path) -> sqlite3.Connection:
    """A migrated in-memory copy of the store, or an empty store when there is
    none, for reading without changing anything on disk (1.7)."""
    copy = connect(Path(":memory:"))
    if path.exists():
        source = sqlite3.connect(path)
        try:
            source.backup(copy)
        finally:
            source.close()
    migrate(copy)
    return copy


def open_store(
    path: Path, migrations: Sequence[str] = MIGRATIONS
) -> sqlite3.Connection:
    """Open and migrate the store at `path`."""
    conn = connect(path)
    try:
        migrate(conn, migrations)
    except BaseException:
        conn.close()
        raise
    return conn

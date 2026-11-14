CREATE TABLE runs (
    id TEXT PRIMARY KEY, requester TEXT NOT NULL DEFAULT 'user',
    kind TEXT NOT NULL DEFAULT 'standard' CHECK(kind IN ('standard','currency')),
    status TEXT NOT NULL CHECK(status IN
        ('running','waiting','interrupted','complete','partial','budget_exhausted','discarded')),
    revision TEXT NOT NULL DEFAULT '', started_at TEXT, ended_at TEXT, process_group INTEGER,
    overrides TEXT NOT NULL DEFAULT '{}', budget TEXT NOT NULL DEFAULT '{}',
    selection TEXT NOT NULL DEFAULT '[]', snapshot_path TEXT, wait_until TEXT, notice TEXT,
    statistics TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE steps (
    run_id TEXT NOT NULL REFERENCES runs(id), key TEXT NOT NULL, phase TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','done','failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0, input_ref TEXT, result TEXT, reason TEXT,
    PRIMARY KEY(run_id, key)
);
CREATE TABLE sessions_seen (
    source TEXT NOT NULL, session_id TEXT NOT NULL, thread_id TEXT, harness TEXT, model TEXT,
    provider TEXT, local INTEGER CHECK(local IN (0,1)), project TEXT, domain TEXT, workflow TEXT,
    interactive INTEGER CHECK(interactive IN (0,1)), started_at TEXT,
    lens_versions TEXT NOT NULL DEFAULT '{}', entries_read INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'read' CHECK(state IN ('read','skipped','failed')),
    reason TEXT, forgotten INTEGER NOT NULL DEFAULT 0 CHECK(forgotten IN (0,1)),
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK(availability IN ('available','unavailable','changed')),
    PRIMARY KEY(source, session_id)
);
CREATE INDEX sessions_thread ON sessions_seen(thread_id);
CREATE TABLE calls (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_key TEXT NOT NULL,
    attempt INTEGER NOT NULL, role TEXT NOT NULL, backend TEXT NOT NULL, model TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    usage_estimated INTEGER NOT NULL DEFAULT 0 CHECK(usage_estimated IN (0,1)),
    seconds REAL NOT NULL DEFAULT 0, session_ref TEXT, outcome TEXT, raw_output TEXT, error TEXT,
    input_ref TEXT, FOREIGN KEY(run_id, task_key) REFERENCES steps(run_id, key),
    UNIQUE(run_id, task_key, attempt)
);
CREATE TABLE patterns (
    id TEXT PRIMARY KEY, key TEXT NOT NULL, lens_scope TEXT NOT NULL DEFAULT '{}',
    created_at TEXT, last_reviewed_run TEXT REFERENCES runs(id), UNIQUE(key, lens_scope)
);
CREATE TABLE observations (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), source TEXT NOT NULL,
    session_id TEXT NOT NULL, entry_id TEXT NOT NULL, source_location TEXT NOT NULL,
    representation_version INTEGER NOT NULL, redacted_quote TEXT NOT NULL, kind TEXT NOT NULL,
    modality TEXT NOT NULL CHECK(modality IN ('asserted','hedged','tentative')),
    excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)), outcome TEXT NOT NULL,
    outcome_evidence TEXT NOT NULL DEFAULT '[]', lens_id TEXT NOT NULL, lens_version TEXT NOT NULL,
    pattern_id TEXT REFERENCES patterns(id), backend TEXT NOT NULL, model TEXT,
    subject TEXT, project TEXT, domain TEXT, observed_at TEXT,
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK(availability IN ('available','unavailable','changed')),
    FOREIGN KEY(source, session_id) REFERENCES sessions_seen(source, session_id),
    UNIQUE(source, session_id, entry_id, lens_id, lens_version, redacted_quote, kind)
);
CREATE INDEX observations_pattern_session ON observations(pattern_id, source, session_id);
CREATE TABLE file_findings (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), repo TEXT NOT NULL, revision TEXT NOT NULL,
    path TEXT NOT NULL, location TEXT NOT NULL, lens_id TEXT NOT NULL, lens_version TEXT,
    finding TEXT NOT NULL, project TEXT, domain TEXT, observed_at TEXT, backend TEXT, model TEXT,
    pattern_id TEXT REFERENCES patterns(id),
    availability TEXT NOT NULL DEFAULT 'available'
        CHECK(availability IN ('available','unavailable','changed'))
);
CREATE TABLE research_findings (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), question TEXT NOT NULL,
    finding TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]', backend TEXT, model TEXT
);
CREATE TABLE knowledge_items (
    id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL,
    scope TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('candidate','active','contested','superseded','dismissed')),
    summary TEXT NOT NULL, confidence TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
    confidence_basis TEXT NOT NULL, supporting_evidence TEXT NOT NULL DEFAULT '[]',
    contradicting_evidence TEXT NOT NULL DEFAULT '[]', latest_support_at TEXT,
    lens_id TEXT, model TEXT, pattern_id TEXT REFERENCES patterns(id),
    run_id TEXT REFERENCES runs(id), feedback_id TEXT,
    actor TEXT NOT NULL CHECK(actor IN ('dream','user','feedback')), base_version INTEGER,
    pending_reassessment INTEGER NOT NULL DEFAULT 0 CHECK(pending_reassessment IN (0,1)),
    imported INTEGER NOT NULL DEFAULT 0 CHECK(imported IN (0,1))
);
CREATE TABLE knowledge_revisions (
    item_id TEXT NOT NULL REFERENCES knowledge_items(id), version INTEGER NOT NULL,
    kind TEXT NOT NULL, scope TEXT NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('candidate','active','contested','superseded','dismissed')),
    summary TEXT NOT NULL, confidence TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
    confidence_basis TEXT NOT NULL, supporting_evidence TEXT NOT NULL DEFAULT '[]',
    contradicting_evidence TEXT NOT NULL DEFAULT '[]', latest_support_at TEXT,
    lens_id TEXT, model TEXT, run_id TEXT REFERENCES runs(id), feedback_id TEXT,
    actor TEXT NOT NULL CHECK(actor IN ('dream','user','feedback')), base_version INTEGER,
    pending_reassessment INTEGER NOT NULL DEFAULT 0, created_at TEXT,
    PRIMARY KEY(item_id, version)
);
CREATE TABLE feedback (
    id TEXT PRIMARY KEY, revisions TEXT NOT NULL, run_ref TEXT, session_ref TEXT,
    outcome TEXT, basis TEXT NOT NULL CHECK(basis IN ('user-confirmed','check-confirmed','unknown')),
    check_name TEXT, correction TEXT, scope TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE currency_findings (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), target TEXT NOT NULL,
    claim TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
    UNIQUE(target, claim)
);
CREATE TABLE changes (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), pattern_id TEXT REFERENCES patterns(id),
    currency_finding_id TEXT REFERENCES currency_findings(id), repo TEXT NOT NULL, branch TEXT,
    commit_sha TEXT, external_input INTEGER NOT NULL DEFAULT 0 CHECK(external_input IN (0,1)),
    applied_at TEXT, reverted_at TEXT, status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','applied','waiting','withheld','rejected','failed')),
    rank_signals TEXT NOT NULL DEFAULT '{}', depends_on TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]', reason TEXT, authoring_id TEXT
);
CREATE TABLE adoption (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), repo TEXT NOT NULL,
    branch TEXT NOT NULL, step TEXT NOT NULL
        CHECK(step IN ('checking','checked','merging','merged','deploying','deployed','reverted')),
    head_before TEXT, head_after TEXT, at TEXT NOT NULL, outcome TEXT
);
CREATE TABLE registrations (
    tool TEXT NOT NULL, run_ref TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL,
    correlation_id TEXT, workflow TEXT, project TEXT, user_quote_entries TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(tool, run_ref, source, session_id),
    FOREIGN KEY(source, session_id) REFERENCES sessions_seen(source, session_id)
);
CREATE TABLE learning (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), scope TEXT NOT NULL,
    evidence TEXT NOT NULL, next_step TEXT NOT NULL, outcome TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'open'
        CHECK(disposition IN ('open','dismissed','deferred_until','addressed')),
    deferred_until TEXT, reason TEXT
);
CREATE TABLE replays (
    change_id TEXT NOT NULL REFERENCES changes(id), authoring_id TEXT NOT NULL,
    source TEXT NOT NULL, session_id TEXT NOT NULL, backend TEXT NOT NULL, model TEXT,
    judgement TEXT NOT NULL CHECK(judgement IN ('recurs','resolved','unclear')),
    baseline_recurs INTEGER NOT NULL CHECK(baseline_recurs IN (0,1)),
    tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(change_id, authoring_id, source, session_id)
);

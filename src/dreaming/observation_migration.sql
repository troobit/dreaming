ALTER TABLE observations ADD COLUMN observation_type TEXT NOT NULL DEFAULT '';
ALTER TABLE observations ADD COLUMN pattern_key TEXT NOT NULL DEFAULT '';
ALTER TABLE observations ADD COLUMN declared_scope TEXT NOT NULL DEFAULT '{}';
ALTER TABLE observations ADD COLUMN source_entry_id TEXT;
ALTER TABLE patterns ADD COLUMN merged_into TEXT REFERENCES patterns(id);
ALTER TABLE knowledge_items ADD COLUMN user_resolved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_revisions ADD COLUMN user_resolved INTEGER NOT NULL DEFAULT 0;
CREATE TABLE review_decisions (
    run_id TEXT NOT NULL REFERENCES runs(id), operation_key TEXT NOT NULL,
    operation TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL,
    item_id TEXT, PRIMARY KEY(run_id, operation_key)
);

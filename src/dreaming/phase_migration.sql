ALTER TABLE observations ADD COLUMN imported INTEGER NOT NULL DEFAULT 0 CHECK(imported IN (0,1));
ALTER TABLE file_findings ADD COLUMN imported INTEGER NOT NULL DEFAULT 0 CHECK(imported IN (0,1));
ALTER TABLE research_findings ADD COLUMN task_key TEXT;
ALTER TABLE research_findings ADD COLUMN evidence_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE research_findings ADD COLUMN external_input INTEGER NOT NULL DEFAULT 0 CHECK(external_input IN (0,1));
ALTER TABLE changes ADD COLUMN edits TEXT NOT NULL DEFAULT '[]';
ALTER TABLE changes ADD COLUMN backend TEXT;
ALTER TABLE changes ADD COLUMN model TEXT;

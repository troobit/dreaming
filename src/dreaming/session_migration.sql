ALTER TABLE sessions_seen ADD COLUMN document_path TEXT;
CREATE TABLE session_inputs (
    call_id TEXT NOT NULL REFERENCES calls(id),
    source TEXT NOT NULL, session_id TEXT NOT NULL,
    PRIMARY KEY(call_id, source, session_id),
    FOREIGN KEY(source, session_id) REFERENCES sessions_seen(source, session_id)
);

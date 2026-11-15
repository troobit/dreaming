CREATE TABLE replay_batches (
    change_id TEXT NOT NULL REFERENCES changes(id), authoring_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','passed','held')), reason TEXT,
    PRIMARY KEY(change_id, authoring_id)
);

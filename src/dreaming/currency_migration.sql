CREATE TABLE currency_pages (
    run_id TEXT NOT NULL REFERENCES runs(id), url TEXT NOT NULL, title TEXT, retrieved_at TEXT,
    text TEXT, status TEXT NOT NULL CHECK(status IN ('fetched','unavailable')), reason TEXT,
    PRIMARY KEY(run_id, url)
);

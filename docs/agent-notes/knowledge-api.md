# Knowledge operations: `dream api` and exchange

Task 21 of `specs/dreaming/tasks.md`: `api.py` (envelope, recall, search, get, feedback), `exchange.py` (export, import) and `dream api` in `cli.py`. Decisions Q114–Q120.

## The envelope

- `api.handle(conn, cfg, request, now=...)` is the testable entry; `api.main(stdin, stdout)` opens the home and store and writes one compact JSON line. `dream api` exits 0 on success, otherwise the error's exit code (2 for request errors) after writing the response.
- Codes: `invalid_request`, `incompatible_version` (envelope version, export schema version, or a store newer than the program), `unknown_op`, `invalid_scope`, `not_found`, `stale_version`, `rejected` (a Reviser rule refused a correction), `run_unfinished`, `invalid_export`, `invalid_config`, `migration_blocked`.
- `dream.*` and `session.*` ops are task 22; add them to `api.OPS`.

## Retrieval

- `knowledge.retained(conn)` gives each item's latest revision whose `originating_run` is NULL (feedback, import) or ended `complete`/`partial`/`budget_exhausted`. Item status comes from that revision, `pending_reassessment` and `imported` from `knowledge_items`.
- Default filter: knowledge `active`, learning disposition `open` today, scope applies (`knowledge.scope_applies`), and not withheld. `include` opens `candidate`, `contested`, `superseded`, `dismissed`, `unresolved`, `out_of_scope`, `withheld`, `deferred`, `addressed`.
- Withheld = `knowledge.revision_withheld`, skipped for imported rows and false for any item with a `user` revision.
- Order is `knowledge.retrieval_key`; `_within` adds entries until the next one's `estimate_tokens(compact JSON)` would pass the limit, and reports `estimated_tokens` and `omitted_count`.
- Evidence references come from `knowledge.evidence_refs`, which reads `SHOWN_OBSERVATIONS` (imported included, excluded and forgotten left out). The author uses the same function with `counted=True`.
- Search matches every lower-cased query term in the content and returns summaries without evidence.

## Feedback

- Shape is validated before the transaction; inside it a known `feedback_id` returns `changed: false` without writing anything (tests compare `conn.total_changes`).
- Every named revision must exist; every named item is marked pending. The reviewer shows the latest five feedback entries on pending items (`review._feedback`).
- `correction` → `Reviser(actor="user", feedback_id=...)`: `revise` (content and/or scope, confidence `high`, status forced `active`) or `dismiss` (optionally `exclude_evidence`, which sets `observations.excluded` on the item's supporting observations and queues other items citing them). `base_version` must equal the current version (`stale_version`). The `user` actor skips the confidence cap and the revision cap, may set a global scope, and makes `Reviser.user_settled` true from then on.
- `learning` → `knowledge.set_learning_disposition`. Correction and disposition requests are kept as JSON in `feedback.correction`.

## Exchange

- `exchange.TABLES` lists tables in foreign-key order with primary keys and JSON columns. Runs export only `RUN_COLUMNS`; the `imported` column is never exported, which is what makes export → import → export identical.
- Export refuses while any run is running, waiting or interrupted.
- Import checks each row's primary key and inserts with plain `INSERT`. Do not switch to `INSERT OR IGNORE`: it also skips NOT NULL and CHECK violations silently, which hides a broken export.
- Imported evidence is filtered out of `COUNTED_OBSERVATIONS` and counted file findings, so it never counts toward promotion; imported runs are filtered out of `run_records`, `unreviewed_dreams` and `run.find_run`'s latest run.

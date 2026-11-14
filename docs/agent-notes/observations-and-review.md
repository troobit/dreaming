# Observations, knowledge rules and review

Tasks 15–19 of `specs/dreaming/tasks.md`: `redact.py`, `observe.py` (gate, Read steps, reader trial), `knowledge.py` (rules, `Reviser`, learning dispositions) and `review.py` (clustering, Review step). Decisions Q96–Q107.

## Observations (`observe.py`, `redact.py`)

- `gate(raw, entries, lenses)` is the whole program-side check, raising `errors.Rejected(reason)`: the entry must be in the part the reader saw, the lens must be one it was asked for, the quote must `verify` (whitespace-normalised substring) against the full entry text, and `user_statement` needs an entry presented as `user`. Only then is the quote redacted.
- Outcomes are downgraded to `unknown`, never rejected, when the cited entry is not the user's (`user_confirmed`) or does not hold a `tool_result` block or role `tool` (`check_confirmed`). Codex puts tool results inside assistant entries, so "check" is decided by block type, not role.
- Evidence ids hash (source, session, location, lens, lens version, redacted quote), matching the table's UNIQUE constraint, so `INSERT OR IGNORE` makes rereads idempotent.
- `redact` loops until a fixpoint; termination holds because every pattern needs a literal (`Bearer`, `://`, a keyword…) that `[redacted]` does not contain. The source module in agentic-coding has no licence file (Q107).

## Read steps (`observe.ReadPhase`)

- `plan(ctx)` is the run's planner: one Read step per selected session (input_ref = source, id, start, copies and attribution as JSON), one per unparseable session (input_ref carries `skipped`), then `review` depending on all of them. It is not yet installed in `run.PLANNER`; wiring belongs to task 22/31.
- `prepare` re-opens the document from apsis (or the registered file) every time, including on resume, and rebuilds `SelectedSession` from the input_ref. Parts are calls `read:<hash>#p<n>`; a crash after a call reuses the stored answer.
- The forgotten flag is checked before the first part and before each later part; a forgotten session ends the step `skipped` with nothing stored.
- `run.Phase` gained an optional `failed(conn, step, reason)` hook that runs in the failure transaction; the Read phase uses it to record the session `failed` (4.7), so default selection passes it over and only an explicit reread retries it.

## Knowledge rules (`knowledge.py`)

- `judge` decides status from the evidence set only: contested when counted contradicting evidence exists and no explicit user statement settles it; active when `support_holds`; otherwise candidate. `split_evidence` moves `failed`-outcome observations cited for a `workflow` item into contradicting (13.3); learning items count only user statements (`item_support`).
- `Reviser` applies reviewer operations (and later feedback, task 21) inside the caller's transaction. It never trusts the model's status: it recomputes it, caps confidence (`confidence_cap`), accumulates evidence across revisions and writes an append-only revision with actor, base version, run, backend, model, lens and latest support date.
- A stale `base_version` or the revision cap marks the item `pending_reassessment` before raising `Rejected`; the step's transaction still commits, so the mark persists.
- "User-settled" means the current revision cites a counted asserted `user_statement`; contest, dismiss and supersede of such an item need a user statement too, and supersede always does.
- `scope_applies` and `retrieval_key` are the scope matching and ordering the knowledge operations (task 21) should use; neither is called by production code yet.
- Ids for created records are `new_id(prefix, run, seed…)` hashes, so re-applying a stored answer after a crash creates the same rows.

## Review step (`review.py`)

- The deterministic pass runs in `prepare` but writes only in `apply`: `Reviewed.automatic` holds the exact-key assignments, applied even when no reviewer call is made.
- No call is made when there are no new clusters, no exact-key additions and no pending or revalidation items; the step finishes `done` with reason "nothing new to review".
- Existing patterns are ordered `(not due, -score, id)`; due means new evidence this run, an item pending reassessment or due for revalidation, or `boost_after_dreams` dreams since the last review. `last_reviewed_run` is set for shown and newly assigned patterns.
- Cluster refs are `<lens>/<key>`; the reviewer uses them in `assignments`, item `pattern` and candidate `pattern`. `_Applier.resolve` maps refs and merged pattern ids to live patterns.
- Candidate positions and signals are computed in `apply`, after assignments, from `knowledge.pattern_signals`, so new patterns rank too.
- Follow-up steps: `research:<question id>` (input_ref = question id) and `author:<candidate id>` depending on its research steps. `run.PHASES` has no research or author handlers yet (task 20); tests stand them in with `runkit.LocalPhase`.
- Run records given to the reviewer are counts per earlier run (steps by phase and state, observations, rejections, candidates, changes); step reasons are left out because they can name sessions (2.11).

## Not yet done here

- Feedback shown to the reviewer for items pending after feedback (11.7) waits for the feedback operation (task 21).
- Before/after counts on earlier changes (11.1) and re-candidacy after an applied change (11.2) belong to task 27; until then a pattern with an applied change is refused as a candidate (Q104).
- Selection warnings, unmapped domains and origins are not stored for the report; task 29 needs to capture them from the planner's `Selection`.

## Tests

- `tests/dreamkit.py`: `ScriptedModel(answer)` parses the prompt's data block and answers per role; `dream(...)` runs Read and Review with local stand-ins for research and author.
- The review-level shuffle property passes even if `review.cluster` stops sorting, because `knowledge.counted_observations` orders rows by date and id; the cluster-level property in `test_read.py` is the one that catches an unsorted cluster.

# Sessions, registration and forgetting

Covers `sessions.py` selection/recording/registration (tasks 6, 7) and the forgetting half of `knowledge.py`. The apsis client itself is in `foundation.md`.

## Selection (`sessions.select_sessions`)

- Listing rows are grouped into one interaction by correlation id (`thread_id`, else a Codex rollout's own session id, Q79). The content copy is the first row by `_CONTENT_RANK`: harness session, then `codex-exec` (no prompt), then the LM Studio log.
- Selection only reads the store and apsis; the Read step records what was read, so a dry run changes nothing.
- `sessions_seen.entries_read` is an index into `document.entries`, including unattributed LM Studio entries, which `SelectedSession.entries()` filters out afterwards.
- Migration v2 added `size_bytes`/`request_count` to `sessions_seen`, so an unchanged processed session is passed over without opening it. When the listing has neither value, the document is opened and an empty `entries()` drops it.
- Registered files (source `file`, id = resolved path) are re-read from disk on every selection (`_file_rows`); a missing or invalid file is reported as skipped.

## Recording

- `record_session` and `record_skipped` do not open a transaction: the Read step applies results and inserts follow-up steps in one transaction (design, Steps). `register_sessions` and `forget_session`/`restore_session` open their own with `store.transaction`, so never call them inside another transaction (a nested `BEGIN` fails).
- Every copy gets the interaction's attribution and its own listing size; only the content copy gets `entries_read` and `lens_versions`. Recording never touches `forgotten`.

## Registration (`sessions.register_sessions`, 14.8, 2.10)

- Every request is validated before anything is written. The `registrations` row stores only the correlation id the caller gave. Copy lookups use that id, or a derived one: the file document's correlation id, or a Codex session id.
- Idempotence tests compare `conn.total_changes`, which counts even a no-op `UPDATE`, so the "unchanged" path must not write anything.
- A late registration only turns `user_statement` observations into `tool_prompt` (except user-quote entries). It never promotes `tool_prompt` to `user_statement` for newly quoted entries, because the reader saw those entries as role `tool`. It sets `interactive = 0` and the workflow on `sessions_seen`, but does not re-derive project or domain for sessions already read (no config is passed in).

## Forgetting (`knowledge.py`, 2.11)

- `knowledge.COUNTED_OBSERVATIONS` is the single source for counts, rankings, rates, replay selection and any observation handed to a role. It drops `excluded` evidence and forgotten sessions at query time, so forgetting needs no derived state, and restore reverses it exactly.
- `session_copies` matches the named session, any row whose `thread_id` equals the id or the correlation id, and rows sharing the named row's `thread_id`. Forgetting through any copy marks all of them.
- `item_withheld`/`change_withheld` mean the counted supporting evidence fails 4.5/13.3 now, whatever the cause. An item resting only on a hedged user statement in fewer than `min_sessions` sessions is withheld even with nothing forgotten.
- Not yet implemented: a running dream re-checking the flag before each call, and excluding earlier dream call sessions whose input contained a forgotten session. The run engine stores whatever a phase passes as `ctx.call(..., input_ref=...)` on the `calls` row; the Read phase (task 16) decides that format, so both belong there (see `run-engine.md`).

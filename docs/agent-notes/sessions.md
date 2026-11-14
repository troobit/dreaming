# Session planning and evidence exclusion

- `select_sessions` is read-only. It opens apsis documents before grouping by
  recorded `thread_id`, prefers harness content, and attaches server request
  metadata. Missing correlation identifiers are never guessed. Warnings and
  unattributed log entries go to `Selection.diagnostics` only. Prior dream
  sessions carry `subject=dreaming`; later reader/observation code must limit
  their evidence to dreaming's own behaviour.
- Selection orders by modification time, falling back to creation time. Date
  bounds use creation time, with an inclusive start and exclusive end. Date,
  project and explicit reread filters revisit content. Otherwise an unchanged
  applicable lens-version map uses each representation's own entry cursor.
- `record_selection` persists parse failures separately. Call
  `mark_read(store, selected, config)` only after the complete session succeeds;
  it records source copies and their separate cursors in one short transaction.
  Read steps checkpoint individual parts and commit the final cursor with their
  observations using `mark_read(..., conn=conn)`. No Git subprocess
  runs inside these write transactions.
- `reader_document` rechecks forgetting and registration immediately before a
  call. `reader_parts` uses it and the smallest configured reader fallback
  context, counting serialized metadata, entries and truncation markers after
  reserving prompt/output tokens. Entry IDs are not unique in apsis v1; use
  `entry:N` locations in the original document, preserved through filtering.
- Registration accepts source/id pairs or a validated shared-json file path.
  File paths are referenced, not copied; they must remain readable. Registered
  sources enter selection even outside normal apsis discovery. Registration
  applies across known source copies, changes automated prompt observations to
  `tool_prompt`, preserves explicitly quoted user entries, and flags dependent
  items `pending_reassessment`. That flag is the reviewer's durable queue.
  Observations keep canonical `entry:N` plus `source_entry_id` so registration
  by a source's original id still works. User-confirmed outcomes are rechecked
  against registration in the live view; automated prompts cannot confirm use.
- `knowledge.py` supplies live observation, pattern/ranking, retrieval-support
  and adoption-support views. Forget/restore flags every known thread copy in
  one transaction, queues dependent items and keeps all data and retained
  statuses. Support is checked dynamically, so restore reverses withholding.
  Late registration alone marks knowledge pending without withdrawing it.
- The run engine must call `record_call_inputs` after inserting a call row and
  before dispatch, including session origins of summaries and indirect inputs.
  This allows `blocked_call_refs` to exclude derived transcripts and run records
  transitively when their input is forgotten. Recheck live views before every
  call/adoption; never cache them across calls. Applied changes are untouched.
- These are library operations. CLI/API wiring, reports,
  rate measurement and replay execution remain later phases. Their queries
  must use the live exclusion views rather than read evidence tables directly.

# Research and authoring

- `authoring.py` supplies Research/Author handlers for the existing Engine and
  Dispatcher. Both use snapshotted roles and counted inputs, record source
  session lineage, and checkpoint backend/model provenance for recovery.
  Research findings never become session observations or fresh recurrence counts.
- Author preflight reopens sessions through apsis or registered shared-json
  documents, checks quote, location and attribution, and checks file findings
  against their configured repository's committed default branch. File locations
  use `file` or one-based `line:N[-M]`. Changed or missing evidence defers with
  its reason. Root CLAUDE.md/AGENTS.md blobs are explicit, counted inputs.
- Calls expose Read only and return deltas; this phase never changes a checkout.
  `before_apply` rechecks source availability and exact preimages outside a write
  transaction, even when recovering a saved response. The result transaction
  stores changes with unique edit ids, supporting and distinct counter cases,
  dependencies, ranking and author provenance. It rejects whole-file rewrites,
  ambiguous preimages, path escapes, symlinks and dependency cycles.
- `change_id(run_id, author_key)` lets an author cite planned changes before
  their proposals have been saved. `linked_groups` computes dependency components
  for adoption. `adopt.py` consumes stored deltas into private worktrees; checks,
  replay and group adoption run outside the Engine's application transaction.
- Research permitted web/PR tools is conservatively external input because the
  current adapter does not expose durable tool-result telemetry. That flag and
  cited web sources propagate to changes and cannot be cleared by author output.
  Local-only research can cite repository/revision/path/location sources.
- Interactive skills are deferred with their name. Learning recommendations
  require user learning evidence, live scope and promotion eligibility; their
  stable pattern/scope identity preserves existing dispositions on later dreams.

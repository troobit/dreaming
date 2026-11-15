# Run engine and versioned installation

- `RunSession` takes the non-blocking run lock before opening a writable store.
  `prepare` either snapshots a new run or restores an unfinished run's snapshot,
  selection, overrides and budget. Backend choices are the only resume override.
  Standalone probe rows lack a snapshot and are never selected for resumption.
- `detach("module:function", run_id)` transfers the same lock description to a
  new session. A pipe barrier prevents work until the parent records the process
  group. Worker entry points take `(store, run_id)` and supply phase handlers to
  `Engine`. All other subprocesses must close file descriptors.
- `Steps` owns stable keys and dependency validation. Application callbacks use
  the supplied connection, never open nested Store transactions, and return
  follow-up Steps. Evidence writes, follow-ups and completion share one commit.
  A handler's `prepare` returns call metadata, including input-session lineage;
  `call` returns `CallResult`; `apply` receives the durable redacted raw response.
- Resume applies saved successful responses without a model call, even after
  budget exhaustion. A call dispatched without a saved response is indeterminate
  and stops as partial instead of generating a second answer. An optional
  `Dispatcher` owns each attempt's call rows and shares the engine's budget and
  interrupt flag. Its handler `prepare` returns role, prompt, schema, tools,
  optional lineage and a checkpoint callback. The callback verifies reader
  quotes before redaction and persistence. `operations.work` wires the public
  detached start/resume path, restores source references and schedules Adopt
  after Review's Research/Author follow-ups in the same transaction.
  Handlers with `resize` receive the smallest live fallback context; a reduced
  model context resizes the pending input before retrying.
- `StepDeferred` records a preflight reason without making a model call. A
  handler's optional `before_apply` hook runs outside the write transaction,
  including on recovery, so reopening evidence or reading Git cannot hold a
  database writer across subprocesses. Its result is then applied atomically.
- Budgets sum every recorded attempt. Active seconds checkpoint at step/call/wait
  boundaries; waits and downtime do not count. A hard kill loses time since the
  last checkpoint. Readers preserve reserved tokens and a distinct-session cap;
  split parts use explicit input lineage to avoid counting one session twice.
  Interrupts are checked between steps. The finalizer makes no model calls;
  the public worker finalizer writes the styled report on every exit path.
  Explicit resume uses `allow_new=False`, so an exhausted run cannot create an
  unintended replacement run. Automatic start can begin a new bounded dream.
  Dispatcher calls retain redacted complete request envelopes for role replay.
- Dry planning uses committed lenses and an in-memory store snapshot; only
  executable `--version` checks and existing probe caches are consulted. The
  rune plan is a replaceable view of steps, never execution input.
- `make install` archives the clean committed tree into an immutable revision
  environment. A standalone stdlib launcher chooses a run's recorded revision
  before importing a package or migrating. Installs use a separate lock, so
  adoption can install while a worker holds the run lock. Pruning takes both
  locks and retains `current` plus every unfinished run's revision; it removes
  only directories with a matching installation manifest.

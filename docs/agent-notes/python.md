# Python implementation rules

- SQLite allows concurrent readers with a writer under WAL; it still allows
  only one writer at a time. Do not hold write transactions across subprocesses.
  Use `isolation_level=None` and explicit transactions. Avoid `executescript`
  inside an atomic migration: it can commit the pending transaction first.
- Python's `bool` subclasses `int` and `True == 1`. Check `type(value) is int`
  for version and numeric configuration fields that must reject booleans.
- A child can close stdout and continue running; subprocess monitoring needs a
  deadline on both pipe reads and the final `wait`. Handle a final JSON event
  without a newline. On macOS a killpg racing with an already-exited leader can
  return EPERM; check/reap the child before treating that as a live kill failure.
- Validate packaged data through an installed wheel outside the checkout.
  Source-tree imports alone do not verify that SQL, JSON Schema and licence
  resources ship correctly.
- For a UTF-8 token estimate, serialize the entire JSON payload before counting
  bytes. `len(str)` counts code points; escaping, metadata and envelopes also
  consume input space. Preserve original entry indices when filtering records.
- SQLite `mode=ro` can still create WAL/SHM sidecars or update shared memory.
  `snapshot_connection` copies a stable DB/WAL pair into a private temporary
  directory and backs it up into memory for inspections that must change no
  persistent state. Ignore access times when checking copy stability.
- A SQLite connection context manager commits/rolls back; it does not close
  the connection. Close snapshot and temporary connections explicitly.
- `flock` follows an open-file description shared through `pass_fds`. Closing
  the parent's descriptor preserves the child's lock; `LOCK_UN` would release
  it for both. Mark the inherited descriptor non-inheritable in the worker.

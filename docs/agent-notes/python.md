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

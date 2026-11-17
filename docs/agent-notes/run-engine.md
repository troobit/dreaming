# Run engine: steps, calls, budget, lock, dry run, versions

Tasks 8–10 of `specs/dreaming/tasks.md`: `steps.py`, `budget.py`, `run.py`, `plan.py`, `versions.py`, and `dream run` / `dream status` in `cli.py`.

## How a run executes

- `run.start` (the invoking process) takes `~/.dreaming/lock` with a non-blocking `flock`, creates or resumes the run row inside one transaction, spawns `python -I -m dreaming.run <id> --lock-fd N --ready-fd R` with `pass_fds` and `start_new_session=True`, records the child's pid as `runs.process_group`, writes one byte to the ready pipe, then closes its own descriptors. Tests pass `child=[...]` to swap in fake child programs; fakes ignore their arguments.
- The ready pipe closes the gap between spawning and recording the group: the child blocks on `--ready-fd` before doing anything, so a resume can always find the group to kill. If `start` dies or the `UPDATE` fails first, the child reads end of file, logs it and exits 1 with nothing done; the run stays `running` and the next start resumes it.
- The child (`run.child_main`) marks the descriptor non-inheritable, waits on the ready pipe, re-locks the lock descriptor (a no-op on the same open file description), builds a `Runner` from the run's snapshot config plus `runs.overrides` (`run.run_config`), and exits with `run.exit_code(status)`. Its stdout and stderr go to `runs/<id>/run.log`.
- `run.wiring(home, row, cfg)` builds the child's handlers from the run's snapshot (`<snapshot>/instructions`) and `Apsis.from_config`: `author`, `adopt` (with a `ReplayGate`) and, by `runs.kind`, either `read`/`review`/`research` with `ReadPhase.plan` as planner, or `research` = `CurrencyPhase` with `currency.plan`. The report is `report.Writer(home, cfg)`. A `DreamingError` while wiring (for example a snapshot without role prompts) leaves the run `interrupted` with the message and exits 1.
- Tests that start the real child need the shipped instruction set (`runkit.instruction_set`) and the `fakes` fixture for `apsis`; set `report.open_browser = false` when the requester is `user`, or the child opens a real browser.
- A phase is an object with `prepare(ctx, step)` (outside any transaction; makes calls through `ctx.call`) and `apply(conn, step, prepared) -> Outcome` (inside the step's transaction). The runner adds `Outcome.follow_up` and marks the step in that same transaction. An optional `failed(conn, step, reason)` runs in the transaction that marks a step `failed` after `InputFailed`.

## Run kinds

- `runs.kind` is `dream` or `currency`; `run.start(..., kind=)` and `create_run(kind=)` set it, a resume keeps it. `dry_run(kind="currency")` lists `cfg["currency"]["targets"]` and never calls apsis.
- `steps.wait_for(conn, run, key, keys)` adds dependencies to a pending step in the caller's transaction. A currency research step uses it so the planner's `adopt` step waits for the Author steps it adds later; `next_ready` would otherwise run `adopt` first because it has the lower `seq`.

## Exactly-once calls

- `ctx.call(role, prompt, schema, part=...)` keys each call as `step.key` or `step.key#part`. If a `calls` row with that key and `outcome = 'ok'` exists, its `raw_output` is returned and no model is asked. That is how a crash between the call and the commit resumes without a second, different answer.
- `ctx.attempt` writes the calls row before the call and the raw output right after it (autocommit). The retry and fallback dispatcher wraps `attempt`, not `call`, so every attempt gets its own row and `budget.tokens_used` (the sum over `calls`) includes retries (see `backends-and-roles.md`).
- `steps.recover` runs at the start of every `execute`: `running` steps go back to `pending`, and calls without `ended_at` become `interrupted`.
- `tests/test_steps.py::test_resume_after_any_crash_matches_an_uninterrupted_run` uses a `Crash(BaseException)` that nothing catches, so open transactions roll back as if the process died. Disabling the stored-output lookup makes it fail, which confirms the test can detect a double call.

## Budget

- Limits are checked before each model-phase step and in `attempt` before every call (Q83). Total limits (tokens, wall clock) raise `LimitReached(exhausted=True)`: the step is `skipped`, model steps after it are skipped, `adopt` steps still run, and the run ends `budget_exhausted` (exit 0).
- The Read share (Q84) only skips Read steps; the run carries on to Review.
- Wall-clock time is `runs.active_seconds`, saved in each step's transaction. `ctx.wait_until` pauses it, sets the run `waiting` with `wait_until`/`wait_notice`, sleeps in 10 s chunks while checking `runner.stopping`, then sets it back to `running`. The dispatcher's usage-limit wait calls it.
- Tests use `runkit.FakeClock` (time moves only when a fake call takes time or the run sleeps) and `FakeModel`, whose answer counts the calls it has answered.

## Lock and survivors

- A resume that gets the lock while the row says `running`/`waiting` calls `kill_survivors(process_group)`: it kills the group only if the leader pid is gone, because a live leader means the pid was reused. In tests the killed child must be reaped (`process.wait()`) first, otherwise its zombie looks alive.
- `backends.run_process` puts each call in its own session, so calls are outside the run's group and a resume does not kill an orphaned call (Q88).

## Dry run and plan view

- `run.dry_run` reads the store through `store.memory_copy` (an in-memory backup, migrated), never `open_store`, so it creates no db, home or WAL file and changes no row. `memory_copy` backs up from a private copy of the db and its `-wal`, because opening the real store would write its `-shm` and checkpoint a WAL left by a killed dream into the db (`python.md`). Backend status comes from `probe.cached_status` (`--version` plus the cache).
- `plan.write` renders `runs/<id>/tasks-dream.md`; failed and skipped steps are `[x]` with the state in the title, running is `[-]`. Nothing reads the file back.

## Versioned installs

- `make install` runs `python -m dreaming.versions install`: it builds `git archive HEAD` (the committed tree, not the working tree) into `versions/<12-char sha>` with `uv venv` + `uv pip install`, writes `dreaming-revision` into the venv last (a directory without it is rebuilt), repoints `current`, writes the `~/.dreaming/bin/dream` launcher, links `~/.local/bin/dream` to it, and prunes versions no unfinished run records. It refuses an existing `~/.local/bin/dream` that is not its own link, such as an old `uv tool install`.
- `versions.revision()` reads `sys.prefix/dreaming-revision`; outside an install it is `dev`, which is what tests and `uv run dream` record.
- The launcher runs `current/bin/python -I -m dreaming.versions launch`, which execs `versions/<rev>/bin/dream` for the newest unfinished run, else `current/bin/dream`. `-I` matters: plain `-m` puts the working directory on `sys.path`, so running from a checkout's `src/` would import the checkout instead of the installed version. The detached child uses `-I` and `cwd=home` for the same reason.

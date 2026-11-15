# Foundation modules

- `config.py` validates the external TOML configuration, provides role fallback
  lists and scope rules, and snapshots effective configuration plus committed
  instructions. Snapshot reads pin the default branch to a commit first; no
  working-tree file is copied. Private names are read separately and their
  contents never enter the snapshot. Paths in TOML resolve from its directory.
- `require_external` resolves symlinks, checks `.git` markers, and invokes Git
  with repository override variables removed. It rejects bare repositories,
  worktrees, and an ambiguous Git failure. `ensure_home` enforces mode 0700;
  the database is mode 0600 and its resolved path is checked separately.
- `Store` uses WAL, foreign keys, a five-second busy timeout, autocommit, and
  explicit short transactions. The original schema covers the design's 17 tables;
  migration 2 adds registered-document paths and call input lineage.
  Migrations only create tables/indexes or add columns, run atomically, and
  refuse while a running/waiting/interrupted/partial run exists.
- `sessions.py` consumes apsis shared-json v1, validates against the vendored
  schema, and exposes typed documents. Unknown v1 fields/blocks are accepted;
  source omissions remain `None`. There is no source-format parser here.
- apsis uses Go's flag parser: put all flags before the session identifier.
  Its help examples with flags after an identifier do not select shared-json.
  Saved exec streams need the full identifier returned by the listing, not
  only the thread id. Native Codex rollouts can suppress their exec listing.
- `ApsisClient(codex_exec_dirs=...)` adds those directories alongside existing
  `APSIS_CODEX_EXEC_DIRS`. Future session selection must pass the private
  `DREAMING_HOME/codex-exec` directory to include past dream calls.
- `probe_backend` records call rows before execution and usage/raw redacted
  output afterward. It records probes under a run id, caches by version and
  backend configuration, and can share a caller's run id and budget. Dry probes
  run only `--version` and read caches. Standalone probes have their own rows;
  standalone probes are not resumable dreams (they have no snapshot). The Run
  engine handles locking/resumption for runs with snapshots.
- `backends/process.py` monitors actual invocation events, not tool definitions,
  kills the process group on agent events/timeouts, and bounds captured output.
  Codex tool namespaces require descending into their `tools` array.
- Installed Codex 0.169.0 still exposed user instruction/skill blocks and
  `spawn_agent` in a read-only `debug prompt-input` check with the proposed
  suppression flags. Treat this as a failed isolation probe, not proof that a
  flag works. Authenticated model-call probes remain opt-in.
- Current public CLI commands include `start`, `resume`, `discard`, `register`,
  `forget`, `restore`, `status [--plan]`, `api`, `probe`, `run --dry-run` and
  `trial reader --backend NAME --sessions N`. `api` exposes knowledge and run/session operations with
  versioned JSON and opens read requests through a read-only snapshot. Backend adapters,
  direct tool-free LM Studio probing/calls, role definitions, versioned
  installation and the bootstrap launcher are available as library operations.
  Migration 5 adds a durable replay disposition per change and authoring.

Use Makefile targets for checks. `make test` includes isolated wheel installation
and import of packaged SQL/schema resources. `make test-live` makes real model
calls; it is intentionally excluded from the regular suite.

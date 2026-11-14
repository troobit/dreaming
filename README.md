# Dreaming

Dreaming learns from locally recorded agent sessions. The Foundation phase
provides the installable `dream` command, private SQLite storage, configuration
snapshots, backend capability probes, and an apsis shared-json client. The
remaining pipeline is tracked in [the spec](specs/dreaming/tasks.md).

The Sessions library phase adds scoped incremental selection, thread deduplication,
context-sized reader parts, attribution and session registration. Its reversible
forget/restore operations exclude evidence from support and ranking while keeping
stored data. These operations will be exposed through the later CLI/API phase.

The Run engine library phase adds transactional steps, durable call responses,
budget accounting, detached locking, and resume from saved configuration and
selection. Phase handlers and the complete model pipeline are added by later
tasks. Preview selection and backend availability with `dream run --dry-run`;
inspect recorded progress with `dream status` or `dream status --plan`. The plan
is generated from SQLite for reading with `rune list`.

The Backends and roles library phase adds recorded retries and fallback, schema
validation, a circuit breaker, and interruptible provider-limit waits that exclude
sleep from the active-time budget. It provides isolated Claude/Codex adapters,
a direct tool-free LM Studio adapter, and snapshot-based role prompts, schemas
and versioned lenses. MCP calls pass through a filter exposing only the role's
allowlist. Codex roles are refused when their probe cannot establish the required
tool restrictions. The full reading and knowledge pipeline remains in later phases.

Requires Python 3.12+, uv, and Git on a local POSIX filesystem. Install with
`make install`, or use `uv run dream` during development.

Versioned installation requires a clean committed checkout. It installs that
revision under `DREAMING_HOME/versions/`, switches `current`, and links the
launcher at `~/.local/bin/dream`. The launcher selects the recorded revision for
an unfinished run. Existing unrelated commands are refused. Set
`DREAMING_BIN_DIR` when invoking Make to choose a different launcher directory.
Old versions remain available; `uv run python -m dreaming.install --prune`
removes only unused managed versions while no dream holds the run lock.

Copy `instructions/config.example.toml` to `~/.dreaming/config.toml` and set
the instruction repository and its default branch. `DREAMING_HOME` overrides
the private data directory, which must be outside every Git repository.

```sh
dream --version
dream probe --dry-run
dream probe --backend claude
dream run --dry-run
dream status --plan
```

`probe --dry-run` checks executable versions and reads cached results without
creating the private home or calling models. A real probe calls the selected
harnesses, records token usage, checks instruction and tool isolation, and
verifies that apsis can open the resulting session. Results are cached by
backend version and configuration. Tool-using LM Studio calls run through a
harness; configure a Codex backend with `provider = "lmstudio"` and a loaded
model. A direct `harness = "lmstudio"` backend serves only tool-free roles and
probes the already-loaded model without inference. Its dry probe makes no HTTP
request. `LM_API_TOKEN` supplies authentication when the local server requires it.

Run `make test`, `make lint`, `make format`, and `make build` for development.
`make test` uses fake harnesses and synthetic upstream contract fixtures. When
apsis is installed, it also verifies discovery of a synthetic saved exec stream.
`make test-live` opts into real model calls and installed-tool checks; tools
without a login are skipped. Set `DREAMING_LIVE_LMSTUDIO_MODEL` to enable the
LM Studio case. A live isolation failure is reported as a failed test.

The MIT-licensed apsis schema and fixtures have their provenance in
[the fixture notes](tests/fixtures/apsis/README.md).

# Dreaming

Dreaming learns from locally recorded agent sessions. The Foundation phase
provides the installable `dream` command, private SQLite storage, configuration
snapshots, backend capability probes, and an apsis shared-json client. The
remaining pipeline is tracked in [the spec](specs/dreaming/tasks.md).

The Sessions library phase adds scoped incremental selection, thread deduplication,
context-sized reader parts, attribution and session registration. Its reversible
forget/restore operations exclude evidence from support and ranking while keeping
stored data. These operations will be exposed through the later CLI/API phase.

Requires Python 3.12+, uv, and Git on a local POSIX filesystem. Install with
`make install`, or use `uv run dream` during development.

Copy `instructions/config.example.toml` to `~/.dreaming/config.toml` and set
the instruction repository and its default branch. `DREAMING_HOME` overrides
the private data directory, which must be outside every Git repository.

```sh
dream --version
dream probe --dry-run
dream probe --backend claude
```

`probe --dry-run` checks executable versions and reads cached results without
creating the private home or calling models. A real probe calls the selected
harnesses, records token usage, checks instruction and tool isolation, and
verifies that apsis can open the resulting session. Results are cached by
backend version and configuration. LM Studio probes run through a harness;
configure a Codex backend with `provider = "lmstudio"` and a loaded model.

Run `make test`, `make lint`, `make format`, and `make build` for development.
`make test` uses fake harnesses and synthetic upstream contract fixtures. When
apsis is installed, it also verifies discovery of a synthetic saved exec stream.
`make test-live` opts into real model calls and installed-tool checks; tools
without a login are skipped. Set `DREAMING_LIVE_LMSTUDIO_MODEL` to enable the
LM Studio case. A live isolation failure is reported as a failed test.

The MIT-licensed apsis schema and fixtures have their provenance in
[the fixture notes](tests/fixtures/apsis/README.md).

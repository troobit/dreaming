# Foundation: home, store, config, probes, apsis client

Tasks 1–5 of `specs/dreaming/tasks.md`.

## Layout

- `src/dreaming/config.py` holds the private home checks as well as config, lenses and the snapshot (the design has no `home.py`).
- `src/dreaming/errors.py`: exit codes (0, 1, 2, 4, 5) and `DreamingError(message, exit_code, code)`; `code` is the stable `dream api` error code (`invalid_config`, `migration_blocked`, `incompatible_version`, `apsis_failed`, ...).
- `src/dreaming/backends/__init__.py`: `run_process` runs every subprocess in its own process group with `close_fds=True` and kills the group on timeout. Later adapters should use it so the dream lock is never inherited.
- `backends/claude.py`, `codex.py`, `lmstudio.py` hold the command builders and parsers the probe uses and the adapters' `call()`; see `backends-and-roles.md`.

## Store (`store.py`)

- Connections use `sqlite3.connect(..., autocommit=True)` and explicit `BEGIN IMMEDIATE`/`COMMIT` via `store.transaction()`. Under the default legacy mode, `executescript` commits any open transaction first, which would make migrations non-atomic.
- `migrate` checks `user_version` without a lock first, so opening an up-to-date store never waits on a running dream's write transaction.
- Migrations must be additive; `test_every_migration_statement_is_additive` enforces CREATE TABLE/INDEX or ALTER TABLE ADD COLUMN only.
- Observations are de-duplicated by `UNIQUE (source, session_id, entry_id, lens, lens_version, quote)`; insert with `INSERT OR IGNORE`.

## Config (`config.py`)

- Fixed sections are validated by merging over `DEFAULTS` (unknown keys and wrong types exit 2). Open sections (`backends`, `roles`, `classification`, `origins`, `domains`, `repositories`) have their own validators.
- User classification rules are prepended to the defaults; the first match wins.
- A direct LM Studio backend (`harness = "lmstudio"`) is rejected for any role whose tool list is non-empty (5.1).
- `write_snapshot` reads `instructions/` with `git ls-tree`/`git show` at the configured branch's commit, never the working tree, and records the commit as `instructions.revision` in `snapshot/config.json`. The names list is never copied; only its path is in the config.

## Probes (`backends/probe.py`)

- Canary files (`CLAUDE.md`, `AGENTS.md` holding `DREAMING-CANARY: <hex>`) sit in `probes/<backend>/` and its `work/` cwd; a model repeating the marker means project instructions loaded.
- Claude: tries `--safe-mode`, then `--setting-sources "" --disable-slash-commands` with `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. The tool list, skills and MCP servers come from the stream-json `init` event, so those checks are deterministic. The prompt goes on stdin because `--tools` and `--mcp-config` take a variable number of values.
- Codex: `codex debug prompt-input` (no model call) is checked for markers first (`# AGENTS.md instructions`, `### Available skills`, `Deferred tool namespaces`, `spawn_agent`). Only a view without blocking leaks gets a real `codex exec --json` call. `debug prompt-input` accepts only `-c`/`--enable`/`--disable`, not `--oss` or `-m`. The phrase `## Skills` also appears in Codex's base prompt, so it is not a leak marker.
- Codex isolated home: `~/.dreaming/codex-home` with `auth.json` symlinked to the user's. If Codex replaces the link with a file (a token refresh), that isolation set is refused, because the user's copy may now be stale.
- Live findings, 2026-11-14 (Q81): claude 2.1.311 with `--safe-mode` passes every check. codex-cli 0.169.0 lists bundled skills under every flag tried, including an empty `CODEX_HOME`, so `make test-live` fails `test_live_codex` on purpose until that changes. `--disable apps` removes the `mcp__codex_apps__*` namespace.
- Usage accounting: Claude `input_tokens` excludes cache reads and writes (they are added); Codex `cached_input_tokens` is a subset of `input_tokens` (not added).
- Direct LM Studio probe thread ids are `dream-probe-<hex>`. Session selection (task 6) should treat `dream-` thread ids as the dream's own. The probe checks the model is loaded through `/api/v0/models`, never `/v1/models`, which lists every downloaded model when LM Studio loads on demand.

## apsis client (`sessions.py`)

- Fixtures in `tests/fixtures/apsis/` are recorded from synthetic inputs by `tests/apsis_contract.py`; see its README.
- `correlation_id` is `thread_id`, else a Codex rollout's session id (Q79). Claude entries carry no model in apsis 4bf9776 (Q80).
- `--codex-exec-dir` replaces `APSIS_CODEX_EXEC_DIRS`, so `Apsis.from_config` carries the environment value over, then adds the configured dirs and the dream's own `runs/` and `probes/` dirs (5.8).

## Tooling gotchas

- `ruff format .` also formats ```python blocks inside Markdown (ruff 0.17); `specs/` is excluded in `pyproject.toml` for that reason.
- `addopts = "-m 'not live'"` excludes live tests by default; `make test-live` passes `-m live`, which overrides it.
- Fake executables live in `tests/fakes/` and are copied into a temp bin by the `fakes` fixture with a `sys.executable` shebang. A JSON scenario file drives them, and they log each call to a JSONL file.

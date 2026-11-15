# Backends and roles: dispatcher, adapters, roles

Tasks 11–14 of `specs/dreaming/tasks.md`: `backends/__init__.py` (dispatcher), `backends/claude.py`, `codex.py`, `lmstudio.py`, `mcp.py`, `schema.py`, `roles.py` and `instructions/`.

## Dispatcher (`backends.Dispatcher`)

- `Context.call` hands the dispatcher an `attempt` closure; each attempt writes its own calls row, so retries and fallbacks are counted in the budget.
- `Context.attempt` validates every output against the schema itself (`schema.validate`), whatever the backend's structured-output option did. A mismatched output is stored with `outcome = 'invalid'`, so a resume never applies it, then raised as `SchemaError`.
- Two failures on one backend: the last decides. Transport → backend unavailable for the run, next backend gets its own two attempts. Schema → `InputFailed`, the step is `failed`. `breaker_k` schema failures in a row disable the backend; `Runner._retry_tripped` puts the steps that failed on it back to `pending` for the next backend.
- Dispatcher state lives in the process, so a resumed run starts with every backend eligible (5.4).
- Usage limits: `usage_limit_reset(text, now)` is a pure function of the message; `now` is `clock.now().astimezone()` because Claude prints clock-only times in the local zone. A reset within `max_usage_wait_minutes` calls `ctx.wait_until` (run `waiting`, notice on the row, wall clock paused) and repeats the call; it is neither an attempt nor a failure.
- Missing usage is estimated in `run._with_usage` (`ceil(bytes/4)` of prompt and output) and flagged `usage_estimated`.

## Adapters

- All three share the `Backend` protocol: `name`, `context_tokens`, `release_date`, `accepts_session_id`, `probe()`, `call(role, prompt, schema, tools, session_id=None)`. They raise `BackendError` with the raw error text, which the dispatcher classifies.
- `probe.adapter(home, cfg, run_id, result)` builds the adapter with the isolation the probe confirmed (`safe-mode`/`setting-sources` for Claude, `user-home`/`isolated-home` for Codex), or returns None for an unusable result. `run.ProbedBackends` calls it the first time a role asks for a backend; a backend the probe finds unusable is logged and recorded as a `probe:<name>` call with outcome `unavailable`, which the report lists.
- Claude: `--session-id` is chosen before the call (`accepts_session_id = True`) and written to `calls.harness_session_id` first. `--tools` is the full built-in list the model sees, `--allowedTools` pre-approves them and the MCP tools, `--permission-mode dontAsk` refuses anything else instead of prompting. The prompt goes on stdin.
- Codex: the event stream is written to `runs/<id>/codex/<thread>.jsonl` (where apsis reads exec streams), and the call is killed by `run_process(stop_when=launches_agent)` on any agent-launch event, because `spawn_agent` cannot be removed (Q81). The schema file goes under `runs/<id>/codex/schemas/`.
- LM Studio: before every call it reads `/api/v0/models` (`state`, `loaded_context_length`) and sets `context_tokens` from the loaded model. A model that is not loaded, or a prompt larger than the loaded context, fails without sending anything: `/v1` requests would make LM Studio load the model on demand (Q91). The run id is the client thread id and `session_ref`; `request_body` keeps `client_metadata` first so a truncated log entry still names the run. A role with tools raises `DreamingError(invalid_config)`, not `BackendError`, so it stops the run instead of falling back.

## MCP (`backends/mcp.py`)

- Shared definitions come from `mcp.servers_file` (agentic-coding's `mcp/servers.json`). `role_servers` keeps only servers the role names and cuts devtools' `ENABLE_ADDITIONAL_TOOLS` to the role's tools, minus `codex-agent`/`gemini-agent`.
- Secrets are `${SERVER_SECRET}` references for Claude and `env_http_headers`/`env_vars` for Codex (Q89). Unverified assumption: Claude Code expands `${VAR}` in an `--mcp-config` JSON string as it does in `.mcp.json`; check with a live call before relying on the GitHub server.
- Codex's `env_vars` cannot rename a variable, so an env secret whose expected name differs from `SERVER_SECRET` is a config error for Codex only.
- The shared definitions are optional (Q135): `load_servers` returns `{}` when `servers_file` is unset or missing, and `role_servers` leaves out a server they do not define, so the call runs without its tools. `mcp.unavailable_tools(cfg)` names them for the report. `--allowedTools` still lists the dropped `mcp__…` names, which is harmless. An unreadable or malformed file is still `invalid_config`.

## Roles and the instruction set (`roles.py`, `instructions/`)

- `roles.load(root, cfg)` reads `roles/<role>.md`, `schemas/<role>.json` and `lenses.toml` from a snapshot's `instructions/`; tools come from `cfg["tools"]`. A schema keyword `schema.validate` would ignore is refused (`invalid_instructions`).
- Schemas must stay strict-mode compatible (Q92): every object closed with all properties required, `["string", "null"]` for optional values, no `minLength`/`minItems` etc. `tests/test_roles.py::test_schemas_are_accepted_by_strict_structured_output` enforces this. Multi-valued results use arrays (the author's `changes` and `learning` hold zero or one item) instead of nullable objects.
- `Role.prompt(data)`: role text, the schema, then the data as compact JSON between `<dreaming-input>` markers; the end marker is escaped inside the data (Q93). Role prompts must not contain the markers.
- Sizing: `input_tokens(cfg, role, fixed)` = smallest context − 25% for the reply − the fixed prompt. `reader_room` gives the entry budget for `sessions.split_entries`; the reader input's entries render byte-for-byte like `sessions.render_part`, so parts that fit the room fit the prompt.
- `reviewer_input` packs `PatternSummary` dicts: new clusters up to `new_cluster_share` of the room, then existing patterns in the caller's (ranked) order, then run records; the first item that does not fit stops each list, and leftovers are counted (`omitted`, patterns per lens). Evidence reaches the reviewer only through `Evidence.from_row`, which keeps the id and the stored redacted quote cut to `quote_chars`.
- Readers, reviewers and replayers must have empty tool lists (config check, Q94).
- `reader_lenses`: sessions with `subject = dreaming` get only the `dream-behaviour` lens; user sessions get every other lens that applies (Q95).
- Not built yet: the replay judge (a reviewer-role call with its own prompt and schema, task 25). The researcher and author inputs are built in `research.py` and `author.py` (see `research-and-authoring.md`); `roles.fits` checks a whole prompt against the smallest context.

## Prompt sizes (shipped instruction set)

Fixed prompt cost in estimated tokens: reader ~1000 (~1700 with all lenses), reviewer ~2100, author ~1400, researcher ~900, replayer ~300. With an 8k-context local model the reader has about 4300 tokens per part.

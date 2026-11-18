# Tooling inventory and context weight

Task 28 of `specs/dreaming/tasks.md`: `inventory.py`, the Read step's recording, `report.write` and the report's `inventory` section. Tests: `tests/test_inventory.py`. Decisions Q133, Q134.

## Data flow

- `observe.ReadPhase.apply` passes `inventory.session_use(session)` to `sessions.record_session`, which stores it as JSON in `sessions_seen.inventory` on the content copy: `tools` (tool_use names over the entries read), `skills` (Claude Code `Skill` tool inputs) and `first_request` (`measured`, `instructions`, `tool_schemas`).
- `report.write` calls `inventory.record(conn, cfg, run)`, which builds the run's inventory and stores it in `runs.inventory`; `sections.inventory` renders only that JSON. `report.render` alone does not record it, so tests that render must call `inventory.record` first.
- `build` reads the run's `done` Read steps joined to `sessions_seen` (forgotten sessions left out), the shared MCP definitions (`mcp.load_servers`) and each harness's files in `HARNESS_FILES` under the user's home.

## What the numbers are

- `measured`: the first `usage` block's `counters.input_tokens` anywhere in the document. Only Codex exec streams carry usage in apsis 4bf9776, and that count includes the first prompt.
- `instructions`: system entries before the first user entry (LM Studio logs record them), else the harness's instruction files. `skills`: the front matter of each `SKILL.md`, which is what a skill listing loads. `tool_schemas`: the first request's `parameters.tools` (LM Studio logs). Anything else is `unavailable`.
- One context row per harness and model, from the session with the largest weight (measured, else the estimated sum); a harness with files but no sessions gets one row with model `None`.
- Window: the smallest `context_tokens` of configured backends whose `model` matches the session's (case-insensitive); otherwise no flag is possible.

## Gotchas

- MCP use is recognised only by Claude Code's `mcp__<server>__<tool>` names; other harnesses' MCP calls are counted as plain tools.
- Claude Code MCP servers come from `~/.claude.json` (`mcpServers` and `projects.*.mcpServers`); plugins and `.mcp.json` in repositories are not read.
- Unreadable configuration or skill files go to `problems` and the report's "Could not be read" callout; they never fail the run.
- Tests rely on the autouse `isolated_home` fixture: `~` in `HARNESS_FILES` expands to the temporary HOME.

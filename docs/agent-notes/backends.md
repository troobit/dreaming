# Backends and role inputs

- `Dispatcher` records every attempt through `Steps` before dispatch, validates
  role output independently of provider schema support, and shares `RunBudget`
  with `Engine`. Every retry rechecks input lineage, including forgetting.
  Backend availability and schema streaks live in run statistics; construct a
  dispatcher with `resumed=True` and fresh adapters to make backends eligible
  again and probe them.
- Two failed attempts normally stop a schema-invalid input without transport
  fallback. Opening `breaker_k` disables that backend and requeues earlier failed
  inputs for fallback. Failed sessions are marked for explicit reselection.
  Engine records failed-input omissions without applying invalid output, so
  independent reading and dependent review continue; the breaker can requeue
  those omissions. Failed availability probes do not make a successful fallback
  run partial.
- `usage_reset` is pure with an injected aware local clock. Known clock-only
  resets roll to tomorrow; explicit past dates and unknown zones are rejected.
  Waits store reset time and a fallback notice on the run, pause active accounting,
  and poll interrupts. No reset, an excessive wait or an unknown format follows
  transport retry/fallback. The classifier accepts message shapes tested against
  the local Orbit classifiers; no Orbit implementation is imported.
- Harness adapters accept shared MCP registry records or mcpServers mappings.
  Each selected stdio server runs through `mcp_filter`, which filters tools/list,
  rejects unlisted tools/call and blocks server-initiated sampling. Client flags
  alone are insufficient to hide a server's default tool schemas. HTTP MCP
  transport filtering is unsupported; the initial devtools integration is stdio.
- Each CLI call has a private cwd, a stdin prompt (avoiding argv size limits),
  probe-confirmed isolation flags and a process timeout. Codex streams are kept
  both under the run and in the apsis-discoverable codex-exec directory. Claude
  session IDs are recorded before launch; returned model IDs update the call row.
  Codex refuses roles whose required tool restrictions cannot be established.
- Direct LM Studio discovery reads loaded instances at `/api/v1/models`, falling
  back to v0 only on 404/405. It never loads a model. Each call refreshes loaded
  context and puts client_metadata.thread_id first in its completion body. A
  context shrink returns context_changed for the phase to rebuild/split pending
  input. A rejected response_format removes the option on the dispatcher's next
  recorded attempt; program-side validation remains mandatory. LM_API_TOKEN is
  optional authentication. Server logs are the session record.
- `RoleSet` reads only the run snapshot. Schema and tool-name overhead and output
  space count against the smallest fallback context. Reviewer summaries may be
  omitted with counts; source evidence is never silently clipped. Pass live
  `context_limits` after probing and rebuild pending inputs on backend changes.
  The role schemas and eleven initial lenses are editable instruction files.

Endpoint references checked during implementation:
[loaded models](https://lmstudio.ai/docs/developer/rest/list),
[structured output](https://lmstudio.ai/docs/developer/openai-compat/structured-output),
[Claude CLI flags](https://code.claude.com/docs/en/cli-reference).

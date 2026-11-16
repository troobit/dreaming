# Dream reports

- `report.render_report(store, run_id, sections=...)` reads durable run state and
  produces standalone HTML. `write_report` writes it atomically under the private
  home's `reports/` directory with mode 0600. Both reject unsafe identifiers;
  report destinations are checked against normal, worktree and bare repositories.
- The Engine worker supplies `write_report` as its exit finalizer. Report
  generation makes no model calls. Browser opening is explicitly enabled by the
  caller and still restricted to requester `user`; missing browsers only log.
- The optional section map contains trusted internal HTML, keyed by `changes`,
  `effect` or `inventory` (other existing section keys also work). Extension
  renderers must escape their own stored values. Default sections cover retained
  revisions, learning, observations, call usage, candidates, earlier branches,
  forgotten sessions and currency notes, with empty states where appropriate.
- Session usage comes from reader call lineage. Run statistics may override the
  four session counts and supply `selection_skipped` records for skips that never
  made a call. No raw role response, source transcript or configured names list
  is copied into the report. All ordinary stored text is escaped and scrubbed;
  source links render only for HTTP(S) URLs.
- CSS and the page shell are vendored from agentic-coding revision
  `5d07d2f7f31675709f57b35579f72058e846fa67`, with only attribution, formatting and
  footer changes. `report/UPSTREAM.txt` records that upstream declared no licence
  at inspection; no licence grant is invented. Runtime does not need that repo.

# Tooling inventory (`inventory.py`)

- `inventory_section(store, run_id, config, client, home)` opens the run's read,
  non-forgotten sessions through the apsis client and returns trusted HTML for the
  report's `inventory` section. `operations.work` supplies it at finalization; a
  failure only logs, so it never blocks the report.
- Configuration is read from `~/.claude.json` (user and per-project `mcpServers`),
  `~/.claude/settings.json` (`mcp__server__tool` allow rules), `~/.codex/config.toml`
  (`mcp_servers`, `enabled_tools`), plus `CLAUDE.md`/`AGENTS.md`, `rules/` and skill
  frontmatter. Tool names are not enumerated by these files, so "configured tools" is
  often empty; usage supplies the observed ones.
- Labels: tokens from files are `estimated` (bytes/4; skill listings count only name and
  description). Tool schemas are estimated only from LM Studio `requests[].parameters.tools`.
  A `measured` total comes only from a usage block whose basis does not contain `total`
  (Codex thread totals are cumulative and are not treated as first-prompt context); the
  smallest value per harness is used as the closest to a bare baseline.
- Flag: measured, else estimated, total above `thresholds.context_warning_share` times
  the backend's `context_tokens`. "Need" is deliberately narrow: an MCP call that failed
  as an unavailable tool while the server is configured only for a different harness.

# `dream` skill

- `skills/dream/SKILL.md` is the only file in its directory; other harness repositories
  install it by copying the directory. `allowed-tools: Bash(dream:*)` and
  `tests/test_skill.py` enforce that every command in its code blocks is `dream start|run|
  status|resume` or `echo '<json>' | dream api`. It never passes `--requester`, so the
  run records requester `user`.
- The browser opens from the worker at run end (user requester); the skill never opens
  it itself, it reports `report_path` from `dream status`.

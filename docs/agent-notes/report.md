# Dream report

Task 29: the `report/` package, `tests/test_report.py`, helpers in `tests/reportkit.py`. Decisions Q69, Q76.

## How it is built

- `report.Writer(home, cfg)` matches `run.Report` (`(conn, run_id)`): `write`, then `open_report` with the run's `requester`. Not wired: task 31 passes `report=report.Writer(home, cfg)` in `run.child_main`, where home and config are known.
- `write` renders, writes `<home>/reports/<run-id>.html` (dir mode 0700), sets `runs.report_path` (autocommit), returns the path. `config.ensure_home` already refuses a home inside a repository; the writer does not re-check.
- `render` reads only the store, so it can be re-run; an unknown run raises `not_found`.
- `open_report` opens `path.as_uri()` only for requester `user` with `report.open_browser` on; exceptions and a False return are printed (child stdout is `runs/<id>/run.log`), never raised. The browser opens from the detached child, since `dream run` without `--wait` has already exited.

## Sections (`report/sections.py`)

- `SECTIONS` = ordered `(anchor, heading, body)`; body `(conn, run_row, cfg) -> str`; `""` drops the section and its TOC entry. Always shown: statistics, knowledge, learning, changes, effect, inventory, unmerged, config-changes. Only when non-empty: candidates, research, currency, forgotten.
- Task 27 replaces `effect`; task 28 replaces `inventory`. The changes section is task 24's (see `adoption.md`).
- Table cells are escaped unless wrapped in `sections.Html`. `source_link` renders `<a>` only for http/https schemes (per `urlsplit`) and uses `geturl()` as href; other links are shown as text.

## Where the numbers come from

- Sessions: the run's Read steps (`done` read, `skipped`/`failed` with `steps.reason`, `pending` not reached), not `sessions_seen` (one row per copy; `last_run` moves).
- Split: a reader call key ending `#p2`.
- Patterns: `patterns.created_run`; distinct `observations.pattern_id` for the run.
- Tokens: `budget.tokens_used`; per backend/model: `budget.usage_by_backend`; "partly estimated" if any `usage_estimated = 1`.
- Knowledge: `knowledge_revisions.originating_run`, by kind under separate headings. Learning: the `learning` table, research findings in a "Researched, not observed" callout apart from observation ids (15.2).
- Unmerged: other runs (started no later) with a branch and status `proposed`/`waiting` (copy of `review.UNMERGED`; `review` imports `run`). Withheld changes are never committed, so they are left out.
- Config changes: fnmatch on edit paths (`CONFIG_PATHS`): checks, restrictions, tool lists, `instructions/*`.
- Forgotten: all `sessions_seen` rows with `forgotten = 1`.

## Vendored style (`report/shell.py`)

- `CSS` is agentic-coding `scripts/review_html/css.py` verbatim at 5d07d2f7f31675709f57b35579f72058e846fa67 (css.py last changed 3a876e80, template.py e53d1935); no LICENSE in that repository. Extracted with `ast` and compared once. The string ends with `# noqa: E501` (ruff applies it to the whole multi-line string).
- `PAGE` is `template.py` reduced to top bar, title, card grid, TOC, `$sections`, footer; tables reuse the `findings` class.

## Not done

- Selection warnings and unmapped domains/origins are not stored; the planner must persist them for the report to show them.
- 7.4 suggestions for external configuration have no store field.

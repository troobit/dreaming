# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Dreaming run engine (tasks 8–10): `dream run` takes a non-blocking lock, records the run (requester, revision, selection, overrides, budget, configuration snapshot) and continues in a detached child that holds the lock; a second start exits 4 and changes nothing, and the next start resumes an interrupted run, keeping its selection and overrides and accepting only backend choices. `--wait` exits with the run's code (0 complete or budget exhausted, 1 interrupted, 2 usage, 4 already running, 5 partial).
- The step table (`steps.py`): steps with stable keys and dependencies, follow-up steps committed with each result, a `calls` row written before every model call and its raw output stored as soon as it returns, so a resumed run applies stored output instead of calling again.
- Budgets (`budget.py`): tokens summed from recorded calls, a wall-clock limit that does not count time spent waiting for a usage limit to reset, the review and authoring reserve on Read calls, and `budget_exhausted` when the run's own limit stops model work.
- `dream run --dry-run [--json]`: the sessions a start would read, each role's backends labelled cached, unprobed or unavailable, and the budget, with no model call and no change to any file or row.
- `dream status [RUN] [--plan]`: a run's state, and its steps rendered as a rune task file at `runs/<id>/tasks-dream.md`.
- Versioned installs: `make install` builds the committed `HEAD` into `~/.dreaming/versions/<revision>/`, repoints `current`, links `~/.local/bin/dream` to a launcher and removes versions no unfinished run needs; the launcher keeps an unfinished run on the version it started with.
- Store migration v3 (`steps.seq`, `runs.active_seconds`, `runs.error`); dreaming decisions Q82–Q88.

- Dreaming sessions (tasks 6–7): session selection newest first within the session budget, reading only entries added since the last read under unchanged lens versions, with date, project, reread and scope filters (`sessions.select_sessions`). It excludes the running dream's own sessions and recently modified ones, tags earlier dreams' sessions `subject=dreaming`, de-duplicates by correlation identifier (harness content plus LM Studio server metadata), lists unparseable sessions as skipped, and splits sessions into reader-sized parts with truncation markers.
- Session attribution: harness, per-entry model, provider, local/cloud, project (a worktree counts as its repository), domain, workflow and origin. Prompts from non-interactive, registered and dream sessions are presented to the reader as role `tool`, and unmapped domains and origins are listed.
- Session registration (`sessions.register_sessions`), keyed by tool, run reference and session, accepting a source and session id or a shared-json file. Registering again with the same details changes nothing, a forgotten session is refused, and a late registration turns the session's prompts into tool prompts and marks the knowledge it supported pending reassessment.
- `sessions.record_session` and `sessions.record_skipped`, which record what was read on every source copy without clearing the forgotten flag.
- Forgetting and restoring a session (`knowledge.forget_session`, `knowledge.restore_session`) across every source copy. Counts, pattern ranking and observations given to roles leave out forgotten and excluded evidence, and knowledge items or changes whose counted support fails 4.5 or 13.3 are withheld at once.
- `backends.estimate_tokens` (`ceil(utf8 bytes / 4)`), shared by probes and session splitting; store migration v2 adds listing sizes to `sessions_seen`.
- Dreaming foundation (tasks 1–5): the `dream` package (Python 3.12+, uv, no runtime dependencies) with `make test`, `make lint`, `make test-live` and `make install`; the private home (`DREAMING_HOME`, mode 0700, refused inside any git repository); the SQLite store with additive `user_version` migrations, WAL and `migration_blocked`; configuration defaults, validation (exit 2), per-run overrides, lens loading and the start-of-run snapshot read from the instruction repository's default branch; `dream config`; backend probes for Claude Code, Codex and direct LM Studio with a cached dry-run view (`dream probe`, `dream probe --cached`); the apsis shared-json client with recorded contract fixtures.
- Dreaming decisions Q77–Q81: argparse CLI, auto-apply off by default, Codex rollouts correlate by session id, Claude models unknown through apsis 4bf9776, Codex skipped for isolated roles while bundled skills stay listed.
- Specs for `dreaming` (requirements, design, decision log, tasks, prerequisites) and `dry`, plus `specs/BACKLOG.md`.
- Dreaming Decision 16: a provider usage limit with a reset time within `max_usage_wait` makes the run wait and repeat the call; `fallback_on_usage_limit` (default off) moves to the next backend instead, and the waiting notice shows each backend's context size and, where reported, model release date.
- Dreaming design: explanation of the `flock` lock (non-blocking lock taken by the start parent, inherited by the detached child, `close_fds=True` for spawned subprocesses, resume takes the same lock).

### Changed

- Dreaming exit codes: reaching the run's own budget ends as `budget_exhausted` with exit 0; exit 5 is reserved for `partial` runs. Wait time on a usage limit does not count toward wall-clock (requirements 1.2, 5.3).
- Dreaming tasks 2, 3, 8, 11 and 22 updated for the `waiting` run status, `max_usage_wait`, lock behaviour, exit-code mapping and the usage-limit classifier.

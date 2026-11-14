# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Dreaming foundation (tasks 1–5): the `dream` package (Python 3.12+, uv, no runtime dependencies) with `make test`, `make lint`, `make test-live` and `make install`; the private home (`DREAMING_HOME`, mode 0700, refused inside any git repository); the SQLite store with additive `user_version` migrations, WAL and `migration_blocked`; configuration defaults, validation (exit 2), per-run overrides, lens loading and the start-of-run snapshot read from the instruction repository's default branch; `dream config`; backend probes for Claude Code, Codex and direct LM Studio with a cached dry-run view (`dream probe`, `dream probe --cached`); the apsis shared-json client with recorded contract fixtures.
- Dreaming decisions Q77–Q81: argparse CLI, auto-apply off by default, Codex rollouts correlate by session id, Claude models unknown through apsis 4bf9776, Codex skipped for isolated roles while bundled skills stay listed.
- Specs for `dreaming` (requirements, design, decision log, tasks, prerequisites) and `dry`, plus `specs/BACKLOG.md`.
- Dreaming Decision 16: a provider usage limit with a reset time within `max_usage_wait` makes the run wait and repeat the call; `fallback_on_usage_limit` (default off) moves to the next backend instead, and the waiting notice shows each backend's context size and, where reported, model release date.
- Dreaming design: explanation of the `flock` lock (non-blocking lock taken by the start parent, inherited by the detached child, `close_fds=True` for spawned subprocesses, resume takes the same lock).

### Changed

- Dreaming exit codes: reaching the run's own budget ends as `budget_exhausted` with exit 0; exit 5 is reserved for `partial` runs. Wait time on a usage limit does not count toward wall-clock (requirements 1.2, 5.3).
- Dreaming tasks 2, 3, 8, 11 and 22 updated for the `waiting` run status, `max_usage_wait`, lock behaviour, exit-code mapping and the usage-limit classifier.

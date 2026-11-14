# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Installable Python 3.12+ `dream` command with uv packaging, Makefile checks, and isolated wheel-install verification.
- Private storage outside Git repositories, with a WAL SQLite schema, explicit transactions, and atomic additive migrations blocked by unfinished runs.
- Validated TOML configuration, versioned scoped lenses, and snapshots pinned to committed default-branch instructions without copying private names.
- Budgeted Claude Code and Codex capability probes, versioned caches, read-only dry probes, session visibility checks, and process termination on agent-launch events or timeouts.
- Typed apsis shared-json v1 client with pinned MIT-licensed schema and synthetic contract fixtures, plus opt-in live harness checks and implementation notes.
- Specs for `dreaming` (requirements, design, decision log, tasks, prerequisites) and `dry`, plus `specs/BACKLOG.md`.
- Dreaming Decision 16: a provider usage limit with a reset time within `max_usage_wait` makes the run wait and repeat the call; `fallback_on_usage_limit` (default off) moves to the next backend instead, and the waiting notice shows each backend's context size and, where reported, model release date.
- Dreaming design: explanation of the `flock` lock (non-blocking lock taken by the start parent, inherited by the detached child, `close_fds=True` for spawned subprocesses, resume takes the same lock).

### Changed

- Dreaming exit codes: reaching the run's own budget ends as `budget_exhausted` with exit 0; exit 5 is reserved for `partial` runs. Wait time on a usage limit does not count toward wall-clock (requirements 1.2, 5.3).
- Dreaming tasks 2, 3, 8, 11 and 22 updated for the `waiting` run status, `max_usage_wait`, lock behaviour, exit-code mapping and the usage-limit classifier.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Versioned `dream api` knowledge recall, search, get, feedback, export and import, with scoped token-bounded retrieval, quoted evidence, revision history and portable exclusion markers.
- Resumable Research and Author handlers with source citations, live evidence preflight, counted repository instructions, validated delta edits, linked change dependencies and separate learning recommendations.
- Imported-evidence provenance, model-free preflight deferrals, recovery checks outside database write transactions and installed-wheel API validation.
- Verified and redacted observation checkpoints with stable evidence identifiers, source locations, attribution gates, confirmed outcomes, and source-availability markers.
- Resumable reader and reviewer steps with deterministic clustering, bounded review context, scoped knowledge promotion and revision history, protected corrections and dismissals, ranking, reassessment, and atomic research/author follow-ups.
- Read-only `dream trial reader --backend NAME --sessions N` calibration reporting schema validity, quote matches, and token usage without changing the user's store.
- Recorded backend dispatch with bounded retries, fallback, schema validation, circuit breaking, resumable provider-limit waits, and shared engine budget accounting.
- Isolated Claude Code and Codex adapters with role-scoped MCP filtering, stdin prompts, session/model attribution, retained exec streams, and agent-launch termination.
- Direct tool-free LM Studio calls with loaded-instance context checks, early thread metadata, optional authentication, and structured-output fallback.
- Five snapshot-based role prompts and output schemas, eleven versioned initial lenses, and bounded reviewer summaries with explicit omission counts.
- Resumable run engine with stable step dependencies, atomic result/follow-up commits, saved call responses, input lineage, token and active-time accounting, reader reserves, detached lock handoff, and interruption recovery.
- Read-only `dream run --dry-run` selection and backend planning, `dream status --plan` rendering verified with rune, and database inspection that leaves live SQLite files unchanged.
- Immutable revision installs through `make install`, a launcher that pins unfinished runs to their original version, foreign-command protection, and explicit pruning that retains versions needed by unfinished runs.
- Scoped incremental session selection, thread deduplication with server metadata, and reader parts sized for the smallest fallback context with explicit truncation and stable source locations.
- Session attribution and idempotent registration of identifiers or shared-json files, with automated prompt classification and late-registration reassessment.
- Reversible session forgetting across source copies, live support and ranking filters, immediate withholding when support is insufficient, and call input lineage for excluding derived dream evidence.
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

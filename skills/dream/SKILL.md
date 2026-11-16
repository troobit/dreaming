---
name: dream
description: Start, inspect, resume and open the report of a dream, a review of recorded agent sessions that retains knowledge and proposes changes. Use when the user asks to run a dream, check on a dream, resume an interrupted dream, or open the dream report.
allowed-tools: Bash(dream:*)
---

# Dream

Drive the dreaming program through the installed `dream` command. It works from any directory and needs no checkout. Every dream started here is requested by `user`.
Use only `dream` and `dream api`; do not read the dream store or reports directly.

## Start

Preview first when the user is unsure what will be read; no model is called:

```bash
dream start --dry-run
```

Start a dream. It keeps running after this session ends. Narrow it with `--project`,
`--workflow`, `--domain`, `--since` and `--until` (dates), and cap it with
`--max-tokens`, `--max-sessions` and `--max-seconds`:

```bash
dream start
dream start --project /path/to/project --since 2026-11-01
```

The response is JSON with `run_id` and `report_path`. If `ok` is false and `code` is
`already_running`, do not start another; inspect the running dream instead.

## Inspect

```bash
dream status
dream status RUN_ID --plan
```

`run.status` is `running`, `waiting`, `partial`, `interrupted`, `complete` or similar;
`run.notice` explains waits and partial endings.

## Resume

Only an interrupted, waiting or partial dream resumes:

```bash
dream resume RUN_ID
```

## Report

When a user-requested dream ends, the report opens in the default browser. If it did
not, or the user wants it again, run `dream status RUN_ID` and give the user the
`report_path` it returns. `report_path` is null until the report exists.

## Other operations

Machine-readable operations take one JSON request on stdin:

```bash
echo '{"dreaming_api":1,"op":"dream.status"}' | dream api
```

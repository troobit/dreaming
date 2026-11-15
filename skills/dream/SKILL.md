---
name: dream
description: Start, dry-run, inspect or resume a dream, or open a dream's report. A dream reads the user's past agent sessions and retains knowledge about their preferences, workflows and learning opportunities. Use when the user says "dream", "start a dream", "what would a dream read", "how is the dream going", "resume the dream" or "open the dream report". Runs the installed `dream` command, so it works from any directory.
---

# Dream

Every step runs the installed `dream` command. It works from any directory and needs no checkout of the dreaming repository. The dream is recorded as requested by `user`, which is the default, so never pass `--requester`.

If `dream` is not on `PATH`, stop and tell the user dreaming is not installed (`make install` in the dreaming repository installs it).

## Start a dream

Do a dry run first. It lists the sessions the dream would read, the backend for each role and the budget, calls no model and changes nothing:

```sh
dream run --dry-run
```

Show the user what it lists, including any backend marked not usable, and start the dream when they confirm:

```sh
dream run
```

`dream run` prints the run id and returns. The dream keeps running in the background after this session ends, and its report opens in the default browser when it finishes. Do not wait for it.

If the user asks to limit the dream, give the dry run and the start the same options:

| The user asks for | Option |
|---|---|
| only the current repository | `--repo .` |
| another repository | `--repo <path>` |
| one workflow | `--workflow <name>` |
| sessions in a date range | `--since <date>`, `--until <date>` (ISO 8601) |
| reading processed sessions again | `--reread` |
| a smaller budget | `--sessions <n>`, `--tokens <n>`, `--minutes <n>` |
| a review of skills and rules against current sources | `--currency` (takes no session options) |

For example, a dream scoped to the current repository and workflow:

```sh
dream run --dry-run --repo . --workflow <name>
dream run --repo . --workflow <name>
```

If `dream run` says a dream is already running, report that and do not start another. If it says it resumed an interrupted dream, tell the user which selection and overrides it kept.

## Inspect a dream

```sh
dream status
```

It shows the latest dream's state, steps, tokens and running time; pass a run id for another dream. A dream that is `waiting` is paused for a provider usage limit and shows when it continues.

For a tool or script that needs JSON, send this request on standard input to `dream api`:

```json
{"dreaming_api": 1, "op": "dream.status"}
```

## Resume an interrupted dream

```sh
dream resume
```

A resumed dream keeps its selection and budget; only backends may change, for example `dream resume --backend reader=<backend>`.

## Open the report

```sh
dream report --open
```

It prints the latest report's location and opens it in the default browser; pass a run id for an earlier dream. If no browser opens, give the user the printed path.

## Installing in another harness

Copy this `dream` folder into that harness's skills folder. It needs only the installed `dream` command.

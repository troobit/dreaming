# Dreaming

Dreaming reads the agent sessions recorded on this machine and turns what they
show into retained knowledge: the user's preferences and workflows, learning
recommendations, and small evidence-cited changes to skills, rules and
repositories. A dream runs only when asked, in a detached process, within a
token and time budget. It ends with an HTML report. Other tools read and
correct the knowledge through `dream api`.

The specification is in [`specs/dreaming/`](specs/dreaming/): requirements,
design, decision log and tasks. Implementation notes are in
[`docs/agent-notes/`](docs/agent-notes/).

## Requirements

- Python 3.15 or newer, [uv](https://docs.astral.sh/uv/) and `git`. Dreaming has
  no runtime Python dependencies.
- `apsis` on `PATH`, built from the orbit repository. It lists and opens the recorded sessions (Claude Code,
  Codex, Copilot, Kiro and LM Studio server logs).
- At least one backend: the `claude` or `codex` CLI, logged in, or an LM Studio
  server with a model loaded. Every role uses `claude` unless configured
  otherwise.
- A private home on a local POSIX filesystem, outside every git repository:
  `~/.dreaming`, or `DREAMING_HOME`. The run lock uses `flock`, which is
  unreliable on network filesystems.

## Install

```sh
make install
```

This builds the committed `HEAD` of the checkout, not the working tree, into
`~/.dreaming/versions/<revision>/` (under `DREAMING_HOME` when set). It makes that version current and links
`~/.local/bin/dream` to a launcher. An interrupted dream resumes on the version
it started with, so installing never changes a dream in progress. Versions that
no unfinished dream needs are removed. During development, `uv run dream` runs
the checkout instead.

`~/.local/bin` must be on `PATH`. Installing stops if `~/.local/bin/dream`
exists and is not dreaming's launcher, for example after `uv tool install`;
remove it and install again.

## Configure

Copy the example configuration and edit it:

```sh
mkdir -p -m 700 ~/.dreaming
cp instructions/config.example.toml ~/.dreaming/config.toml
```

The example is the author's own setup. `[instructions] repo`,
`[scan] names_file`, `[mcp] servers_file` and `[repositories.agentic-coding]`
point at paths on that machine, and `[adoption] auto_apply = true` merges and
deploys a change once every configured check passes. Change the paths, and
remove `[repositories.*]` or set `auto_apply = false` until you want a dream
to change a repository.

Every setting is optional except `[instructions] repo`, which starting a dream
requires.

- `[instructions]`: the repository whose `instructions/` (role prompts, output
  schemas and lenses) a dream uses, read from the last commit on `branch`. This
  checkout works.
- `[backends.*]` and `[roles.*]`: the harnesses and LM Studio models available,
  and each role's backends in fallback order.
- `[repositories.*]`: repositories a dream may change, with their generate,
  lint, test and deploy commands. A repository without lint and test commands
  only gets proposals. With `[adoption] auto_apply`, a change is merged only
  when every check passes on the merged result.
- `[budget]`: sessions, tokens and wall-clock minutes per dream.

`dream config` prints the effective configuration. `dream probe` checks each
backend's instruction isolation, tools and structured output; `--cached` shows
the last results without a model call.

## Use

```sh
dream run --dry-run          # the sessions, backends and budget a dream would use
dream run                    # start a dream in the background; prints its run id
dream run --repo . --workflow <name>   # scope it to a repository and workflow
dream run --currency         # check configured skills and rules against current sources
dream status                 # state, steps, tokens and time of the latest dream
dream resume                 # resume an interrupted dream; only backends may change
dream discard                # drop an interrupted dream's unmerged branches and records
dream report --open          # open the latest report in the browser
dream forget <session>       # stop counting a session as evidence; `restore` undoes it
dream trial reader --backend <name>   # measure a backend as reader; stores nothing
```

A dry run calls no model and changes no file. A second `dream run` while one is
running exits 4 and changes nothing. A dream paused at a provider usage limit is
`waiting`, and `dream status` shows when it continues. `dream <command> --help`
lists every option.

If an earlier dream is unfinished, `dream run` resumes it with the selection
it started with and ignores new selection options. Discard it first to start
a new one.

The `dream` skill in [`skills/dream/`](skills/dream/SKILL.md) drives these
commands from an agent harness. Copy the folder into the harness's skills
folder to install it.

## `dream api`

`dream api` reads one JSON request on standard input and writes one JSON
response on standard output. Every request names the API version and an
operation:

```sh
echo '{"dreaming_api": 1, "op": "knowledge.recall", "scope": {"project": "/Users/me/repos/app"}}' | dream api
```

```json
{"dreaming_api":1,"op":"knowledge.recall","ok":true,"items":[],"limit":1000,"estimator":"utf8_bytes/4","estimated_tokens":0,"omitted_count":0}
```

A failed request returns `ok: false` with a stable `code` and an `error`
message, and the command exits with that error's exit code (2 for a bad
request):

```json
{"dreaming_api":1,"op":"knowledge.get","ok":false,"code":"not_found","error":"no retained knowledge item k-0123456789ab"}
```

Codes include `invalid_request`, `unknown_op`, `invalid_scope`,
`incompatible_version` (an API, export or store version this program does not
know), `not_found`, `stale_version` (a correction's `base_version` is no longer
current), `run_finished` (resuming or discarding a dream that ended),
`already_running` (exit 4) and `migration_blocked`.

| Operation | Request fields |
|---|---|
| `knowledge.recall` | `scope` (at least one of `domain`, `project`, `workflow`), optional `include`, `limit` (estimated tokens, default 1000) |
| `knowledge.search` | `query`, `scope` (as for recall), optional `include`, `limit` |
| `knowledge.get` | `id` |
| `knowledge.feedback` | `feedback_id` and at least one of `revisions` (`[{"item_id", "version", "use": "cited" or "present_only"}]`), `correction` or `learning`; optional `outcome` (`success`, `failure` or `unknown`; needs `revisions`), `basis` (`user_confirmed`, `check_confirmed` with `check`, or `unknown`), `run_ref`, `session_ref` |
| `knowledge.export` | none; returns the store's retained knowledge as `export` |
| `knowledge.import` | `export`, the document a `knowledge.export` returned |
| `dream.start` | optional `selection` (`since`, `until`, `project`, `reread`, `scope`), `overrides`, `requester`, `kind` (`dream` or `currency`) |
| `dream.status`, `dream.report`, `dream.resume`, `dream.discard` | optional `run_id`; `dream.resume` also takes backend `overrides` |
| `session.register` | `tool`, `run_ref`, `sessions` (`[{"source", "session_id"}]` or `[{"file"}]` naming a session file, each with optional `correlation_id`, `workflow`, `project`, `user_quote_entries`) |
| `session.forget`, `session.restore` | `session_id`, optional `source` |

`dream.start`, `dream.resume` and `dream.discard` take the dream lock. The
other operations take none and may run while a dream runs. Recall and search
return only retained knowledge that applies to the scope, ordered by scope
specificity, confidence and latest support, within the token limit. Feedback
with a `feedback_id` already recorded changes nothing. Registering returns each
session's `state`: `registered`, `updated`, `unchanged` or `forgotten`.

## Develop

```sh
make lint        # ruff check and format check
make test        # the test suite, with fake claude, codex and apsis executables
make check       # both
make build       # the wheel and source distribution, into dist/
make test-live   # opt-in checks against the installed tools
```

uv runs every target on the Python in `.python-version` (3.15.2). `make test`
needs no login, model or network. `make test-live` makes small model
calls with the user's real logins and a temporary `DREAMING_HOME`. Each test
skips itself when its tool is missing: `apsis` for every live test, `claude` or
`codex` for that harness, and an LM Studio server at `http://localhost:1234`
with a model loaded for the LM Studio cases.

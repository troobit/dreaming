# apsis contract fixtures

`listing.json` and `session-*.json` are the shared-json output of the apsis named
in `APSIS_VERSION`, recorded by `uv run python tests/apsis_contract.py` from the
inputs in `inputs/`. Paths under the temporary recording root are rewritten to
`/fixture-root`.

All inputs are synthetic; none was captured from a real session.

| Input | Source |
|---|---|
| `claude/` | Written for these tests |
| `codex/` | orbit `internal/transcript/testdata/codex/turn_context.jsonl`, once with provider `openai` and once served by LM Studio (provider `lmstudio`, originator `codex_cli_rs`, session id `0199cccc-…-03`) |
| `codex-exec/turns.jsonl` | orbit `internal/transcript/testdata/codex_exec/turns.jsonl` |
| `copilot/` | orbit `internal/transcript/testdata/copilot/basic.jsonl` plus a `workspace.yaml` |
| `kiro/basic.json` | orbit `internal/transcript/testdata/kiro/basic.json`, converted as a file (`-a kiro`) |
| `lmstudio/2026-03-05.1.log` | orbit `internal/transcript/testdata/lmstudio/threads.log` with `thread-alpha` renamed to the LM Studio-served Codex session id |

Re-record after upgrading apsis; `make test-live` checks the installed apsis
against these fixtures.

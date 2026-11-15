# Adoption and the replay gate

Tasks 23–25 of `specs/dreaming/tasks.md`: `adopt.py` (work copies, commits, `AdoptPhase`, rollback, report commands), `scan.py` (public scan) and `replay.py` (`ReplayGate`). Tests: `tests/test_commit.py`, `tests/test_scan.py`, `tests/test_adopt.py`, `tests/test_replay.py`. Decisions Q121–Q124 and Decision 17.

## Adopt step (`adopt.AdoptPhase`)

- The `adopt` plan step adds one `adopt:<repo>+<repo>` step per linked group (`linked_groups`, union-find over `changes.depends_on`) that has a committed `proposed` change.
- `_preflight` checks one repository and stops at the first unmet condition, cheapest first: an earlier dream's undeployed merge (`unfinished_adoption`), `auto_apply`, lint/test configured (9.8), `held` (a currency or `external_input` change, 16.3/9.19), `_replays`, `_check`, `_checkout`. The first reason becomes `changes.reason` for every change in the repository, and the whole linked group waits if any member does.
- `_check` builds `merge --no-ff` of the dream branch in a detached `<repo>.check` worktree and runs lint and test there. `_merge` is `merge --ff-only <tested sha>` in the user's checkout, so the deployed commit is the tested one.
- `adoption` rows (`checking … reverted`) are written before the work they name. Whether a merge happened is read from git (`is_ancestor`), not from the rows.
- One `AdoptPhase` instance serves one process. `merged_here` tells a merge it made from one made before an interruption; after an interruption it makes no further merges (9.15).

## Replay gate (`replay.ReplayGate`, 9.16–9.18)

- `adopt.replay_target(conn, cfg, change)`: `None` unless the change cites an observation row. Lens or role-prompt targets (`instructions/lenses.toml` → `reader`, `instructions/roles/<role>.md` → `<role>`) count only in the repository whose path equals `instructions.repo`. Otherwise, any skill/rule/`CLAUDE.md`/`AGENTS.md` path gives `opening`.
- `AdoptPhase(replay=None)` holds every replayable change ("no replayer is set up"). A real run gets `ReplayGate(instructions, snapshot_root, apsis)` from `run.wiring`.
- Sessions come from `knowledge.COUNTED_OBSERVATIONS` by the change's pattern or cited evidence, newest first. Forgotten, dismissed and imported evidence never appear. Each session's quotes go to the judge as `evidence`.
- Opening replays: `opening_prompt` is the first user entry. A block flagged `truncated`/`missing`/`incomplete`, or no user entry at all (Codex exec), makes the session unreplayable. The with-change instruction is `git show <commit_sha>:<path>` for each edited path, each under a `# <path>` heading.
- Reader replays use the latest `done` Read step for `observe.read_key(source, id)` from any run: its `input_ref`, the session reopened through `observe.open_session`, and the scope of the run that made it. The "with" prompts come from a temporary copy of `root` with the changed `instructions/` files written in.
- Every other role prompt is held: its inputs are not stored (Decision 17).
- Call keys are `<adopt step>#<change>:<source>:<session>:<without|with>:<n>` plus `…:judge`. A resumed step reuses stored answers, so it replays once per authoring. Keys must not depend on list position, because a session forgotten between crash and resume would shift positions.
- The judge is `instructions/roles/judge.md` + `schemas/judge.json`, called with role `reviewer`. `recurs_without` is `without == "recurs"` (unclear without does not count, Q123). The with-change `unclear` counts as recurrence in `verdict`.
- Held when: fewer than `replay_min_sessions` plans; remaining tokens < 2 × estimated prompt tokens; a `LimitReached` or `InputFailed` mid-replay; replayer calls on more than one backend; or `verdict` fails. A held change sets the reason on every change in the repository (9.17).
- `AdoptPhase.apply` writes `replays` rows in the step transaction. `report/sections._replay_note` renders them as association.

## Gotchas

- `observe.is_forgotten` takes copies as dicts with `source`, `id` and optional `thread_id`.
- `ScriptedModel` (tests/dreamkit.py) records `tools` per call; replay tests assert the replayer and judge get none.

# The `dream` skill and the portable end-to-end test

Tasks 30–31 of `specs/dreaming/tasks.md`: `skills/dream/SKILL.md`, `tests/test_skill.py`, `tests/test_portable.py`, `tests/portable_model.py`. Decisions Q135–Q138.

## The skill

- `skills/dream/SKILL.md` runs only the installed `dream` command: `run` (with `--dry-run`, `--repo`, `--workflow`), `status`, `resume`, `report --open`, plus one `dream api` request in a ```json block. Other harnesses install it by copying the folder.
- `test_skill.py` parses every line of the ```sh blocks with `shlex` and `cli._parser()`, so a renamed or removed option fails the test. Placeholders like `<name>` parse as plain arguments. Keep commands in ```sh blocks one per line, and `dream api` requests in ```json blocks.
- The skill never passes `--requester` (default `user`, so the report opens at the end) or `--wait` (the dream must outlive the session, 1.1).
- `cli._path` resolves `--repo`/`--project` against the caller's directory because the detached child runs with `cwd` = the home; before this, `--repo .` meant the home.
- `dream report --open` calls `report.open_in_browser` directly, ignoring `report.open_browser`, which only governs the automatic open at the end of a run.

## The end-to-end test (`test_portable.py`)

- Runs `python -I -m dreaming.cli` in subprocesses, so `dream run --wait` goes through the real detached child (`python -I -m dreaming.run`) and every `dream api` call is a separate process. Takes about 2 s.
- Source: the fake `apsis` with `sessions_dir` (scenario key) lists and serves the shared-json documents in that directory; deleting a file makes the session unlisted. Backend: the fake `claude` with `responder` loads `portable_model.py` via `runpy` and calls `answer(prompt, schema)`; it picks the role by a property of the output schema (`observations`, `assignments`, `findings`, `verification`) and returns None for the probe's prompt, which has no `<dreaming-input>` block.
- `portable_model.py` runs inside the fake's process, so it must not import `dreaming`. A new cluster's `key` in reviewer input is already `<lens>/<key>`; use it as the `pattern` reference as is.
- The instruction set comes from `runkit.instruction_set(root)`, a temporary repository holding a copy of the checkout's working-tree `instructions/`.
- Unavailable optional integrations exercised: an LM Studio fallback at `127.0.0.1:9` (probe refused), a missing knowledge folder, no `mcp.servers_file` (the researcher's devtools tools), no harness configuration under the temporary `HOME`, and no repositories. The report must list each.
- The reviewer only adds an `adopt` step when it accepted a candidate, so a dream with knowledge only has Read steps and the review.

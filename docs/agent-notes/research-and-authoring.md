# Research and authoring

Task 20 of `specs/dreaming/tasks.md`: `research.py` (`ResearchPhase`) and `author.py` (`AuthorPhase`, `linked_groups`, `commit_message`). Decisions Q108–Q113.

## Research step (`research.ResearchPhase`)

- `research:<question id>` steps come from the Review step; `input_ref` is the question id. The researcher gets the question, the candidate's id/proposal/summary/repo, configured repositories that exist and knowledge folders that exist.
- Missing repositories and folders are found by the program (`readable`) and stored with the model's own `unavailable` list in `questions.unavailable`; research never waits on them (4.6).
- Findings go to `research_findings` (with `question_id`, `assessment`, `sources` JSON) and never to `observations`; `knowledge.check_evidence` refuses their ids. A finding with no source, no link or passage, or no ISO `retrieved_on` is a `finding` rejection.
- `fetched(source)` decides `research_findings.fetched`: only `repository`/`knowledge_folder` kinds whose link is not a URL (no `://`, no `www.`) count as local. This is a heuristic on the model's output; it cannot see which tools the researcher actually called.
- `equivalent_finding` is ignored here; reusing ids for equivalent findings is the currency dream's job (16.6, task 26).

## Author step (`author.AuthorPhase`)

- `prepare` checks before any call (11.5): `check_evidence` (forgotten or dismissed evidence defers), `support_holds` at the current counts (user statements only for learning), the repository's default branch resolves, and every cited file finding's path still exists there. A failure sets `Authored.deferred` and no call is made; `apply` marks the candidate `deferred` and the step `done`.
- Files are read with `git cat-file blob <rev>:<path>` at the default branch's commit, never the working tree, so the user's uncommitted edits are invisible to the author. `CLAUDE.md`/`AGENTS.md` go into `instruction_files` (counted input, 4.4).
- `_fit` drops file texts largest first into `omitted_files` until `roles.fits` passes; if nothing is left to drop the candidate is deferred.
- `counter_evidence` is counted evidence contradicting non-superseded, non-dismissed items on the candidate's pattern. Every id an answer cites must be in `evidence` (supporting) or `evidence` + `counter_evidence` (counter).
- After the call, `prepare` reads the current text of every edited path so `apply` stays a pure database step.
- Edit rules (`_check_edit`): path inside the repo and outside `.git`; section non-empty, not the file's path or name, and present in the current text (`observe.verify`, whitespace-normalised); a missing file only with `add`; `delete` takes no text, others need text; a supporting case is required.
- Any rule failure discards the whole candidate (`candidates.status = 'discarded'`, an `author` rejection with the reason). The author's own `defer` or `verification.supported = false` defers it.
- A stored change is `proposed` with `evidence` = the change's ids plus every edit's supporting ids, `edits` as given, `research` = finding ids given, `base_revision`, `rank` = candidate position plus signals, and `external_input` (any given finding fetched, or author tools beyond the file tools).
- `commit_message` appends one `Dreaming-Evidence:` trailer per evidence id; file findings add `(repo revision path location)` except in public repositories.
- `deferred_steps` (interactive skill steps, 9.3) are stored on the candidate whatever the outcome.
- `linked_groups(conn, run)` is union-find over the run's changes and their `depends_on`; adoption (task 24) should run one Adopt step per group.

## Gotchas

- Neither phase is in `run.PHASES` yet; wiring is task 31.
- The Claude adapter passes the configured repositories to `--add-dir`, and the author's default tools include `edit` and `write`. Until adoption (task 23) gives the author a work copy as its only writable directory, wiring the Author phase into a real run would let the model write into the user's checkouts.
- `Authored` carries `cfg` because `Phase.apply` gets no context, as `review.Reviewed` does.

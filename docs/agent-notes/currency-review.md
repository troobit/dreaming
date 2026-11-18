# Currency review

Task 26 of `specs/dreaming/tasks.md`: `currency.py` (`plan`, `CurrencyPhase`, source reading and independence), the currency path in `author.py`, retrieval of currency notes in `api.py`. Tests: `tests/test_currency.py`. Decisions Q125–Q130 and Decision 18.

## How a currency run goes

- A run of kind `currency` (`dream run --currency`, or `dream.start` with `kind: "currency"`; no selection allowed). `currency.plan` adds `research:<new_id('t', target)>` per configured target (`input_ref` = the target string) and the `adopt` plan step depending on them.
- `CurrencyPhase.prepare`: resolves the repository with `author.repository` and checks `Repository.exists(path)` at the default branch; a missing target ends the step `done` with the reason (shown under "Targets not reviewed"). Otherwise one researcher call with `target`, `repositories`, `knowledge_folders`, `earlier_findings` (id, claim, assessment, status) and `max_sources` (pages still allowed this run).
- After the call, `_Reader` reads every cited source once per run: `http(s)` pages via the injected `fetch` (default `fetch_page`, urllib + `page_text`), or files inside a configured knowledge folder. Non-public hosts (`localhost`, non-global IP literals), other links, failures and the source limit become `fetched_pages` rows with `error`. The researcher's own `unavailable` list and missing knowledge folders become error rows too.
- `_check`: `research.check_finding`, then `observe.verify(passage, text)` per readable source. One missing passage rejects the whole finding; unreadable sources are just dropped; none left rejects it ("none of its sources could be read"). Rejections are `what = 'currency_finding'`.
- `_Finding.keep` (in `apply`): id = researcher's `equivalent_finding` if it is a finding of the same target, else `new_id("cf", target, same_text(claim))`. `independent()` counts sources; `candidate` needs an assessment in `CHANGE_ASSESSMENTS` and `currency_min_sources` independent sources, otherwise `note` with a reason. Upsert keeps the original `run`.
- A candidate is a `candidates` row (no pattern, `currency_finding_id`, evidence `[finding id]`) and an `author:<id>` follow-up; `steps.wait_for` adds it to the `adopt` step's dependencies.
- The Author step treats it specially (see `research-and-authoring.md`). `adopt.held` keeps every currency change on the branch; the change ends `waiting` with "currency change, never auto-applied (16.3)" when auto-apply and checks are configured, or with the first unmet condition otherwise.

## Identity across dreams (16.6)

- `proposed` findings are not kept again. `dismissed` ones come back only when change-worthy and citing a domain the stored sources did not.
- Before the call, `_deleted_branches` finds the target's `waiting`/`proposed` currency changes whose branch is gone (`adopt.branch_exists`) and whose commit is not an ancestor of the default branch; `apply` marks the change `rejected` and the finding `dismissed` ("its branch was deleted without being merged").
- There is no operation for the user to dismiss a currency finding yet; `dismissed` comes only from a deleted branch or a direct row update.

## Gotchas

- `registrable_domain` is the last two host labels: conservative, never splits one site, but merges e.g. two `*.co.uk` sites and two `*.github.io` sites.
- `fetched_pages` rows count toward `max_sources` only when they hold text and are web links; failures and notes do not.
- A page's `retrieved_on` in the stored finding is the program's fetch date, not the researcher's.
- The researcher never gets the program's copy of a page; the author gets only the passages, inside the data block whose end marker `roles.Role.prompt` escapes.
- `run.wiring` picks `currency.plan` and `CurrencyPhase` (as the `research` phase) for a run of kind `currency`; a currency run has no `read` or `review` handler.

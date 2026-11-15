# Decision Log: Dreaming

## Quick Decisions

| ID | Date | Decision | Rationale |
|----|------|----------|-----------|
| Q1 | 2026-11-04 | Feature name is `dreaming` (`specs/dreaming/`) | The user's term |
| Q2 | 2026-11-04 | Use the full spec lane (requirements, design, tasks) | User-owned ambiguity, persisted store, contested architecture |
| Q3 | 2026-11-04 | A dream starts only on explicit request; no idle or activity monitoring; tools may start dreams per Decision 7 | User direction |
| Q4 | 2026-11-04 | Workers run sequentially; parallel execution is not a design target | The goal is context separation, not speed |
| Q5 | 2026-11-04 | Dreams may change agentic-coding, orbit/apsis and project repositories; the knowledge vault is not a write target | User selected the targets; the vault holds the user's own notes (Q22) |
| Q6 | 2026-11-04 | Default selection is newest unread sessions first within a configurable budget; each run records cost and yield | Batch size is unknown and found by experiment |
| Q7 | 2026-11-04 | Tailoring to the user means editable lenses, scripting repeated steps and spotting stalls; the report is not rewritten in simplified language | User selection |
| Q8 | 2026-11-04 | LM Studio session reading belongs in apsis, not dreaming | Other tools can reuse it |
| Q9 | 2026-11-04 | LM Studio server logs are a session source; LM Studio chat conversations are not | Logs add metadata and direct API use; bodies are truncated |
| Q10 | 2026-11-04 | Earlier dreams are in scope like any session; only the dream in progress is excluded | Prevents recursive reading of its own output |
| Q11 | 2026-11-04 | Web chat apps and interactions with no local record are out of scope | Nothing local to read |
| Q12 | 2026-11-04 | Dreams may target a repository or workflow using only matching sessions and lenses; the specification process is an improvement target | User wants targeted improvement within a general process |
| Q13 | 2026-11-04 | Dreams inspect current skills, rules and configuration for broken or inconsistent references, independent of sessions | User direction; example: recall-knowledge searches a folder capture-knowledge never writes |
| Q14 | 2026-11-04 | Dreams report MCP servers and tools configured versus used, and context spent before the first prompt | Tool bloat hurts local models; LM Studio logs give prompt sizes |
| Q15 | 2026-11-04 | Configured knowledge folders are optional read-only research sources; missing ones are reported and skipped | Research must not depend on an interactive skill |
| Q16 | 2026-11-04 | Reuse existing skills for unattended steps; defer interactive steps and report the limitation | Reuse without an interactive dependency |
| Q17 | 2026-11-04 | Only parallel execution is deferred; measuring effect and linked cross-repo changes stay in | User direction |
| Q18 | 2026-11-04 | Research on every backend uses devtools via a harness with an explicit tool list from the shared MCP definitions | A direct model request cannot run tools (Decision 2) |
| Q19 | 2026-11-04 | Sessions larger than a role's context are split into consecutive parts | Largest sessions are often the most informative |
| Q20 | 2026-11-04 | Configuration is snapshotted at run start; checks and restrictions stay editable | User accepts dreams improving their own checks (Decision 1) |
| Q21 | 2026-11-04 | Section 3 is delivered by a separate spec in the orbit repository | orbit owns `specs/apsis-*` |
| Q22 | 2026-11-04 | No knowledge-vault writes or capture-knowledge dependency | User descoped it |
| Q23 | 2026-11-04 | Run every configured check on the proposed merged result; any failure blocks auto-apply; no baseline runs | One pass/fail rule |
| Q24 | 2026-11-04 | Budgets cover all roles, retries and fallbacks; reserve capacity for review and authoring; finish the report without model calls when the limit is reached | A reading-only budget leaves later work unbounded |
| Q25 | 2026-11-04 | Evaluate changes using later independent sessions; rank recurring patterns and refine existing improvements instead of duplicating | Summaries of earlier evidence are not new support |
| Q26 | 2026-11-04 | Evidence identifiers cover session observations and file findings; verify quotes before redacting stored copies | File-only fixes need evidence; redacted quotes cannot match raw sources |
| Q27 | 2026-11-04 | Label context measurements as measured, estimated or unavailable; external configuration changes are user-applied suggestions | History may lack a breakdown; external configuration has no adoption path |
| Q28 | 2026-11-04 | Linked adoption preflights all repositories and attempts group rollback, reporting every state | Cross-repository atomicity is impossible |
| Q29 | 2026-11-04 | Default selection includes appended session content; explicit rereads revisit history; recovery reverts count as failed adoption, not user rejection | Avoids defeating rereads and wasting the rejection retry |
| Q30 | 2026-11-04 | Budget exhaustion ends a run; crash recovery keeps the remaining budget | Resuming an exhausted run makes no progress; resetting evades the budget |
| Q31 | 2026-11-04 | Every dream call leaves a record the shared reader can find and attribute to the dream; a truncated LM Studio log entry suffices for direct calls | Q10 and 1.5 need dream records readable |
| Q32 | 2026-11-04 | The names list for the public-output scan is read only from files outside every repository | A public list would publish the names (Decision 3) |
| Q33 | 2026-11-04 | After interruption following a first merge into a user checkout, resume makes no further merges or deployments, reports finish and undo commands, and may resume with no budget left | Reporting is safer than automatic finish or revert |
| Q34 | 2026-11-04 | Report merge and revert commands run the repository's generate step before committing, then deploy | Otherwise generated files go stale and lint fails |
| Q35 | 2026-11-04 | A run, including a resumed part, completes on the program version it started with | New code on old state is unsafe; discarding covers a buggy version |
| Q36 | 2026-11-04 | Resume keeps selection, budget and overrides, accepts new backend choices, re-enables failed backends, rechecks and resizes; other overrides are rejected; wall-clock counts only while running | Retrying blocked backends makes no progress; sleep must not exhaust budgets |
| Q37 | 2026-11-04 | Continue the `dreaming` spec as a foundation for personalised tooling beyond agentic-coding | User request |
| Q38 | 2026-11-04 | Recurrence supports frequency; claims that an approach worked also need a confirmed outcome in scope | Repeated untested suggestions must not become effectiveness claims |
| Q39 | 2026-11-04 | Equivalent knowledge keeps its identity and feedback across lenses; explicit user corrections resolve matching conflicts | New identifiers must not bypass corrections |
| Q40 | 2026-11-04 | The next in-scope dream reassesses revision-linked consumer feedback | Closes the personalisation loop |
| Q41 | 2026-11-04 | Unresolved scope is not global; deferred and addressed recommendations are omitted by default | Avoids unintended guidance |
| Q42 | 2026-11-05 | Personal learning covers both how the user works and what the user could learn | User selection |
| Q43 | 2026-11-05 | Provide a small reusable boundary for web or app endpoints; defer UI, server and hosting | Flexibility without added complexity |
| Q44 | 2026-11-05 | Section 3 is delivered by orbit's `apsis-shared-json` and `apsis-lmstudio-logs` specs; criteria 3.1–3.5 match their contracts | Implemented and merged (Q65); dreaming adapts instead of restating |
| Q45 | 2026-11-05 | Classify sessions as local or cloud from recorded provider and model by configured rules (2.2) | apsis records the provider only |
| Q46 | 2026-11-05 | Match duplicate interactions by recorded correlation identifiers; unattributed LM Studio diagnostics stay server-level (2.4) | The log has no request-scoped correlation field |
| Q47 | 2026-11-05 | An evidence location is any location identifying one entry plus the representation version (6.1) | apsis keeps entry identifiers, not source positions; the quote is the content check; an unresolved location defers the candidate (11.5) |
| Q48 | 2026-11-05 | Add search, recall and session-registration operations (14.6–14.8) | Local-model tools need search and recall; runs hand sessions over for indexing |
| Q49 | 2026-11-05 | Retrieval and search limits are in estimated tokens, with the estimate stated | Consumers budget in tokens |
| Q50 | 2026-11-05 | Feedback carries the outcome's basis: user-confirmed, check-confirmed with the check named, or unknown (14.5) | Lets 13.3 use checked outcomes without trusting unverified reports |
| Q51 | 2026-11-05 | Configuration can mark sources and origins non-interactive; Codex exec streams and unmatched LM Studio sessions default to non-interactive; an unmapped origin counts as interactive and is reported (2.9) | Those sources record no origin; tool-written prompts must not count as user statements |
| Q52 | 2026-11-05 | A late registration reclassifies the session's prompts and queues affected knowledge for reassessment (2.10) | Registration is the only guard for tool-started sessions |
| Q53 | 2026-11-05 | A registration can mark entries quoting the user's own words; they count as user statements (2.9, 14.8) | Otherwise the user's relayed words are lost |
| Q54 | 2026-11-05 | Sessions carry a workflow from registration; registration carries source, correlation identifier, workflow and project (2.2, 14.8) | Workflow-scoped knowledge needs the attribute |
| Q55 | 2026-11-05 | Feedback marks each revision as cited or present-only; present-only outcomes are association; a repeated feedback identifier changes nothing (14.5) | Delivered knowledge must not reinforce itself |
| Q56 | 2026-11-05 | For a matched interaction, content comes from the harness or tool session and server metadata from the log; truncated log content only when it is the only copy (2.4) | Exec streams record no prompt |
| Q57 | 2026-11-05 | Retrieval accepts any of domain, project and workflow, returns retained revisions only and states omissions; recall includes status; requester defaults to `user`; only user invocations open the browser (1.1, 10.1, 14.2, 14.7) | Running proposals are not knowledge; dismissed items must not look usable |
| Q58 | 2026-11-07 | Knowledge affected by a late registration is marked pending reassessment and not withdrawn (2.10) | Keeps knowledge usable while exposing doubt |
| Q59 | 2026-11-07 | An active "approach worked" claim needs a confirmed successful outcome; a confirmed failure counts against (13.3) | A confirmed failure must not qualify |
| Q60 | 2026-11-07 | What a later dream may do in a repository an earlier dream merged but did not deploy is a design question, with durable records of adoption steps (Design Handoff) | 9.15 already governs resume |
| Q62 | 2026-11-14 | Session sources also name Copilot, Kiro CLI and Kiro IDE (2.1) | apsis reads all three; Copilot has no origin mapping (apsis Q96), so it counts as interactive and is reported under 2.9 |
| Q63 | 2026-11-14 | A session's model is attributed per entry, falling back to the first recorded model (2.2) | apsis preserves per-entry model changes (Decision 29) |
| Q64 | 2026-11-14 | Decision 6 accepted | Requirements 13 to 15 depend on it |
| Q65 | 2026-11-14 | The orbit baseline is `origin/main` at 4bf9776; both parent specs are complete and accepted, and section 3 is unchanged | Main was reset to a squash of the merged PR |
| Q66 | 2026-11-14 | The program, its instruction set, the `dream` skill and the spec live in one repository, developed on a branch named `dreaming`; a Python 3.12+ package installed with uv | User chose a new repository; follows screw's patterns and keeps the program usable without any other checkout (14.4) |
| Q67 | 2026-11-14 | State, including the steps of a dream, lives in SQLite under `~/.dreaming/`; a rune-format plan is rendered from it for reading | One transactional store; rune renumbers positional IDs and cannot share a transaction with results |
| Q68 | 2026-11-14 | The instruction set is read from the default branch of the instruction repository at run start | Working-tree edits must not change a run, and dreams adopt changes to it |
| Q69 | 2026-11-14 | The report is produced in the exit path, not as a step; the report CSS and shell are vendored once with the source revision recorded | A failed or exhausted run still reports; the package must not depend on agentic-coding at runtime (14.4) |
| Q70 | 2026-11-14 | Observations record `modality` (asserted, hedged, tentative); recall returns the redacted quote and modality with each evidence reference | A 2026 paper reports consolidation turns hedged remarks into confident facts |
| Q71 | 2026-11-14 | Review input is sorted deterministically; a run may supersede or dismiss at most a configured fraction of active items per scope; item versions guard against overwriting a mid-run correction | Consolidation order changes outcomes; OpenClaw caps rewrite loss; the dreaming talk uses optimistic concurrency |
| Q72 | 2026-11-14 | The author emits id-keyed edits citing a supporting and a counter case, not whole-file rewrites | ACE and AutoManual report fewer collapses and fewer hallucinated rules |
| Q73 | 2026-11-14 | Embeddings, graph stores and bi-temporal validity are not adopted | Scoped SQLite suffices at one user's scale; revisions already carry supersession and a latest supporting date |
| Q74 | 2026-11-14 | Currency-review changes stay on the dream branch and are never auto-applied | promoted to Decision 14 |
| Q75 | 2026-11-14 | A forgotten session is excluded and its data kept; a purge operation is a non-goal | User choice; reversibility over erasure (Decision 13) |
| Q76 | 2026-11-14 | The report ships in the MVP before adoption, effect measurement and inventory: task 29 depends only on the run engine and the reviewer, and tasks 24, 27 and 28 add their own report sections | A knowledge-only dream needs its report (10.1) with no repository to adopt into; 10.2 and 10.4 are met in full only when those tasks land |
| Q77 | 2026-11-14 | The `dream` CLI uses argparse and the package has no runtime dependencies | Keeps the wheel portable (14.4) and each versioned install (Decision 11) small; screw's typer adds nothing the commands need |
| Q78 | 2026-11-14 | `adoption.auto_apply` defaults to false in the program; the user's configuration turns it on, as `instructions/config.example.toml` does (9.7) | A missing or partial config never merges or deploys |
| Q79 | 2026-11-14 | A Codex rollout's correlation identifier (2.4) is its session id: apsis records no `thread_id` on Codex rollouts, and Codex sends its session id as LM Studio's client thread id | Contract fixtures recorded from apsis 4bf9776 (task 5); LM Studio and Codex exec documents do carry `thread_id` |
| Q80 | 2026-11-14 | Claude sessions read through apsis 4bf9776 record their model as unknown (2.2) | apsis populates entry models for Codex, Copilot, Kiro and LM Studio but not Claude |
| Q81 | 2026-11-14 | Codex backends stay skipped for roles needing isolation while the probe finds bundled skills in their prompt (5.5) | Probe of codex-cli 0.169.0: `-c skills.include_instructions=false`, an empty `CODEX_HOME` and every other flag tried leave "Available skills" listed; `--disable apps` removes the codex_apps MCP namespace; claude 2.1.311 with `--safe-mode` passes every check |
| Q82 | 2026-11-15 | A step's dependency is met when that step has finished in any state (`done`, `failed` or `skipped`), not only `done` | Review must still run after a Read batch fails (4.7) or is skipped at the Read share |
| Q83 | 2026-11-15 | Limits and stop requests are checked between steps and also before every model call, including retries; a step whose next call is refused is `skipped` with the reason | Satisfies "no call starts once the limit is reached" (Testing Strategy) for steps that make several calls, without cutting off a running call |
| Q84 | 2026-11-15 | The review and authoring reserve (1.2) limits Read calls only: Read steps stop at `tokens × (1 − review_author_reserve)` and are skipped, the run continues, and their sessions stay unread for the next dream | Reaching the share is not a total limit, so the run does not end `budget_exhausted` for it |
| Q85 | 2026-11-15 | A run stopped by an error ends `interrupted` with the error recorded on `runs.error` and exits 1; there is no `failed` run status | Adding a status needs a non-additive change to the `runs` CHECK constraint; an interrupted run can be resumed after a fix on the same revision or discarded |
| Q86 | 2026-11-15 | `dream run` starts the detached dream and prints its id; `--wait` waits and exits with the run's code; `--dry-run` shows the plan instead (task 9's command name) | The detached child keeps running if the waiting client goes away (1.1), and callers that need the outcome still get the exit code |
| Q87 | 2026-11-15 | `make install` builds the committed `HEAD` (via `git archive`) into `versions/<12-char sha>`; while a run is unfinished the launcher sends every invocation to its version, otherwise to `current`; the program refuses to resume a run started on another revision (`wrong_revision`) | A version directory then holds exactly its revision; routing all commands keeps a newer version from migrating the store under the run (Decision 11) |
| Q88 | 2026-11-15 | Killing survivors on resume covers the detached child's process group only, and only when its leader is gone (a live leader means the id was reused); model calls run in their own groups | Calls need their own group to be killed on timeout; an orphaned call holds no lock (`close_fds=True`) and its output pipe is closed |
| Q89 | 2026-11-15 | An MCP server's secret is read from `<SERVER>_<SECRET>` in the environment and only referenced in a call's config: `${VAR}` for Claude, `env_http_headers`/`env_vars` for Codex | Same names as agentic-coding's generated configs; the value never appears in a command line, the fakes' logs or a calls row |
| Q90 | 2026-11-15 | Researchers read pull-request threads through the GitHub MCP server (`github.pull_request_read`, listed in `config.example.toml`); the program's default researcher tools stay devtools-only | Roles get no shell; a default naming the GitHub server would fail every config without it or without a token (4.6) |
| Q91 | 2026-11-15 | The LM Studio adapter and probe read the loaded model and its context from `/api/v0/models` before each call; a model that is not loaded, or an input larger than the loaded context, is a failed call and nothing is sent | `/v1` requests make LM Studio load a model on demand, which dreaming must not do; the user can reload a model with another context between calls |
| Q92 | 2026-11-15 | Role output schemas use only strict structured-output keywords: closed objects, every property required (null for optional), no length or count limits; the program checks lengths and counts itself | Codex `--output-schema` and LM Studio's strict `json_schema` reject other keywords; the program validates every output anyway (4.7) |
| Q93 | 2026-11-15 | A call's prompt is the role prompt, the output schema and the input as JSON between data markers, with the end marker escaped inside the data; inputs leave 25% of the smallest context in the fallback list for the reply | A backend without structured output still sees the shape; session or fetched text cannot close the data block (16.3); sizing for the smallest context lets any fallback take the input (5.2) |
| Q94 | 2026-11-15 | Reader, reviewer and replayer tool lists must be empty, checked when configuration loads | 4.4 gives readers and reviewers no tools and the replayer only the instruction under replay |
| Q95 | 2026-11-15 | A `dream-behaviour` lens reads sessions of earlier dreams, and those sessions are read under no other lens | 1.5: such sessions are evidence about dreaming only, never about the sessions they processed |
| Q96 | 2026-11-15 | Store v4 adds `rejections` (each refused observation or reviewer operation, with its reason and a redacted detail), `candidates` (with rank position and signals) and `questions`, plus `patterns.merged_into` and `learning.pattern_id` | 6.2, 7.5 and 11.6 need refused items with reasons; the Author and Research steps need their inputs by id |
| Q97 | 2026-11-15 | An outcome is kept only when the entry it cites is the user's (`user_confirmed`, `failed`) or holds a tool result (`check_confirmed`, `failed`); otherwise the observation is stored with outcome `unknown`, not rejected | 13.1: the assistant's own claim never confirms, but its quote is still valid evidence |
| Q98 | 2026-11-15 | Unassigned observations whose lens and normalised key equal an existing pattern's join it without the reviewer; shown clusters left unassigned, or assigned to an unknown pattern, get a new pattern; clusters not shown wait for a later review | The key is worded to recur; only the program creates pattern ids (6.5) |
| Q99 | 2026-11-15 | The revision cap allows `max(1, floor(max_revision_fraction × active items in the scope))` supersede or dismiss operations per run; beyond it the operation is rejected with the reason and the item marked pending reassessment | The design's "recorded as candidate" for the next run; a fraction of a small scope would otherwise round to zero and block every dismissal |
| Q100 | 2026-11-15 | A missing scope is taken from the supporting evidence's domains and projects, unresolved when neither is known; a global scope needs evidence from two or more domains or an explicit user statement | 13.2 (source context, never global by default) and 13.4 (no global through recurrence alone) |
| Q101 | 2026-11-15 | Stated confidence is capped by the counts: `high` needs no contradiction and 2 × `min_sessions` sessions, a confirmed outcome or an explicit user statement; `medium` needs support meeting 4.5; the cap is noted in the basis | The design asks the program to check confidence for consistency with the counts |
| Q102 | 2026-11-15 | Revisions accumulate evidence; a `create` equal to an item in the same scope (normalised text) revises it, and one equal to a dismissed or superseded item is rejected; a reviewer dismissal is as final as a user's | 13.5 and 13.9: a new identifier must not bypass earlier dispositions |
| Q103 | 2026-11-15 | Learning items and learning candidates count only user statements as support, and a pattern with a learning recommendation in any disposition gets no new one | 15.1 (tool failures are not evidence of a skill gap) and 15.3 (dispositions are kept) |
| Q104 | 2026-11-15 | A pattern with an applied, unreverted change is not a candidate until effect measurement (task 27) applies 11.2's recurrence rule | Avoids re-proposing changed patterns with no measurement in place |
| Q105 | 2026-11-15 | Reader and reviewer calls record the sessions their input held in `calls.input_ref` (`{"sessions": [[source, id], ...]}`), and selection skips a dream call session whose input held a forgotten session | 2.11; the Read step also re-checks the forgotten flag before each part |
| Q106 | 2026-11-15 | `dream trial reader` reads the N newest sessions (processed or not) through an in-memory copy of the store, calls the chosen backend once per part with no retry or fallback, and takes instructions from the default branch or `--instructions DIR` | Measures the backend alone; makes no store writes (design, Risks and Assumptions) |
| Q107 | 2026-11-15 | `redact.py` copies agentic-coding's credential patterns, adds `sk-`, `github_pat_`, `AIza` and JWT patterns, and repeats until nothing changes; the source repository declares no licence | 12.3 asks for source, revision and licence; repeating makes redaction idempotent by construction |
| Q108 | 2026-11-15 | Store v5 adds research findings by question with their assessment, `questions.unavailable`, `candidates.deferred_steps`, and a change's candidate, summary, commit message, edits, research and base revision; v6 adds `imported` to `runs`, `observations`, `file_findings` and `learning` | Research, authoring and exchange need them; both are additive |
| Q109 | 2026-11-15 | The author reads cited files and `CLAUDE.md`/`AGENTS.md` from the default branch with git, never the working tree; when the prompt does not fit the smallest context, file texts are left out largest first and listed in `omitted_files`, and a prompt that still does not fit defers the candidate | The user's uncommitted edits are not the repository's conventions; the author can read what was left out with its file tools (5.2) |
| Q110 | 2026-11-15 | A research finding counts as fetched unless every source has kind `repository` or `knowledge_folder` and a link that is not a URL; a change is `external_input` when any research it received was fetched or the author's tools go beyond the file tools | The model's label alone could pass fetched text as local; an author with a web or MCP tool may have fetched content the program cannot see (9.19) |
| Q111 | 2026-11-15 | An edit's `section` must appear in the current file, may not name the whole file, and only a file that does not exist may be added whole; an answer that breaks any authoring rule discards the candidate with the reason, and nothing of it is stored | A change is one commit, so a partly valid change would misstate what the evidence supports (design, Adoption step 1) |
| Q112 | 2026-11-15 | The program writes a change's evidence citations as `Dreaming-Evidence: <id>` trailers, a file finding with its repository, revision, path and location, by id only in a public repository; the author's message gives the subject and body | 9.2 needs every change to cite its evidence whatever the model wrote; 9.12 cites by id only in public repositories |
| Q113 | 2026-11-15 | The author receives, beside the candidate's evidence, counted evidence that contradicts knowledge resting on the same pattern (`counter_evidence`); every id an answer cites must be one it was given | The design asks each edit to cite a counter case, which the candidate's own evidence rarely holds |
| Q114 | 2026-11-15 | Retrieval returns an item's latest retained revision: one from a run that ended `complete`, `partial` or `budget_exhausted`, or from feedback or an import; learning recommendations follow the same rule by their run | Q57: running proposals are not knowledge; an interrupted run may still be discarded |
| Q115 | 2026-11-15 | Recall returns open learning recommendations with knowledge items in one ordered list, marked `learning_recommendation`, with research findings apart from evidence and a confidence capped from their counts | 14.2 names learning recommendations in retrieval; 15.2 keeps research apart from observed evidence |
| Q116 | 2026-11-15 | A user correction through `knowledge.feedback` is a revision by actor `user`: it applies at once as active with the stated confidence, may make an item global, and settles the item against inferred evidence; a dismissal may exclude the evidence it rested on, which queues other items citing it | 13.3–13.5: the user's own correction needs no recurrence and is never undone by inferred evidence |
| Q117 | 2026-11-15 | Feedback queues every item it names for reassessment; the reviewer sees the latest five feedback entries on each item pending reassessment, and is told present-only outcomes are association | 11.7 needs the next in-scope dream to reassess against the feedback; 14.5 separates cited from present-only |
| Q118 | 2026-11-15 | `knowledge.export` refuses with `run_unfinished` while any dream is running, waiting or interrupted | An unfinished dream's revisions are not retained (Q114), and leaving them out would leave patterns and evidence it created pointing at rows the export omits |
| Q119 | 2026-11-15 | An import adds rows whose key the store lacks and leaves existing rows alone; imported runs, evidence, items and learning are marked `imported`, imported evidence never counts as support, imported items keep their stated status without being judged again, and imported runs are left out of run records and `dream status` | 14.3 import into a fresh store; merging diverging stores is a non-goal; the marker is not exported, so export after import equals the original export |
| Q120 | 2026-11-15 | `dream api` adds the codes `invalid_request`, `unknown_op`, `stale_version`, `rejected`, `run_unfinished` and `invalid_export`, exits with the error's exit code after writing the response, and recalls at most 1000 estimated tokens by default | Callers need to tell a bad request, an older server and a moved item apart; a small default limit per the design |
| Q121 | 2026-11-15 | A change citing a session observation is replayed when it edits a skill, rule, `CLAUDE.md` or `AGENTS.md` (on opening prompts), or the lenses or a role prompt of the instruction repository (`instructions.repo`); the same `instructions/` paths in another repository are ordinary files | Lenses and role prompts are dreaming's own (7.1); elsewhere those paths mean nothing to a dream (9.16) |
| Q122 | 2026-11-15 | A session's opening prompt is its first user entry; the session is not replayable when it has none, the entry has no text, or any block of it is marked `truncated`, `missing` or `incomplete`. The with-change run's only instruction is the text of the change's edited files at its commit | The markers of 3.4; Codex exec streams record no prompt (3.2); the replayer sees only the instruction under replay (4.4) |
| Q123 | 2026-11-15 | An `unclear` judgement without the change does not count the session as replayed; with the change it counts as recurrence. Replay calls that fell back to another backend hold the change | 9.16 counts a session only when the problem recurs without the change, and asks for one backend; 9.17's unclear rule is about the with-change result |
| Q124 | 2026-11-15 | A replay runs only after the cheaper adoption conditions pass (auto-apply on, checks configured, no 9.19 or 16.3 hold), needs remaining tokens for twice the estimated size of its prompts, and keys each call by change and session under the Adopt step | A replay cannot unhold a branch already held; 9.17 asks for the budget test before spending; stored answers make a resumed step replay once per authoring (9.16) |

---

## Decision 1: Auto-Apply When Every Check Passes on the Merged Result

**Date**: 2026-11-04
**Status**: accepted

### Context

Dreams change skills, rules, MCP configuration and tooling. Harnesses link to the main checkout's working tree, so deploying from a dream worktree would repoint them at a temporary folder. Checks on the branch alone miss conflicts with newer commits on main. The user wants low friction and treats dreaming as trusted automation; checks and restrictions are ordinary improvement targets.

### Decision

Auto-apply is enabled in the user's configuration:
- One branch per repository, one commit per change, generate commands run on the branch.
- Every configured lint and test command runs once on the merge of the branch into the default branch, in a separate working copy. Any failure blocks auto-apply; there is no baseline run. A repository with no configured checks is proposal-only.
- The merge commit is created and tested in the separate working copy and brought into the user's checkout with `merge --ff-only`, only when it is on the default branch, has nothing in the way, and its HEAD has not moved since the checks. The user's checkout therefore receives exactly the tested commit, as one merge commit. Deploy runs from that checkout.
- If deploy fails, revert and redeploy. Linked repositories are checked together and rolled back together.
- No category of change is held for manual approval.

### Rationale

What goes live is what was tested, from the checkout harnesses use. One pass/fail rule is simple, and one commit per change keeps each change and dream revertable.

### Alternatives Considered

- **Proposal only**: Rejected by the user as too much friction.
- **Hold back risky categories**: Rejected by the user in favour of uniform treatment.
- **Merge without deploy**: Rejected: harnesses would be out of step.
- **Check the dream branch alone and fast-forward it**: Rejected: misses conflicts with newer commits on the default branch; deploying from a dream worktree also repoints live links.
- **Protect checks from dream changes**: Rejected by the user.
- **Exempt checks already failing on main**: Rejected: needs baseline machinery and cannot show a worse suite.

### Consequences

**Positive:**
- Improvements reach every harness without a manual step.
- Deployed content equals checked content; each change and dream can be reverted.

**Negative:**
- Passing checks does not prove behaviour improved.
- Auto-apply waits on dirty or non-default checkouts, and unrelated failures block it.
- MCP, hook and permission changes go live unreviewed; run dreaming only where that is acceptable.

---

## Decision 2: Backend-Neutral Roles; Harnesses Run Tool-Using Roles

**Date**: 2026-11-04
**Status**: accepted

### Context

The user runs Claude Code and Codex on subscriptions and local models through LM Studio. Dreaming must not depend on one of them or stop at a usage limit. A direct LM Studio call has no skill loader, file tools, `gh` or MCP loop, and the shared devtools definition includes agent-launching tools that would escape the program's accounting.

### Decision

Reader, reviewer, researcher and author each choose a backend in configuration, with an ordered fallback list and per-run override; calls run one at a time. Tool-using roles run through Claude Code or Codex, on a cloud provider or LM Studio; direct LM Studio calls serve tool-free roles only. Readers and reviewers get no tools; researchers get read, web search and fetch (devtools) and PR-review access; authors get what they need to prepare a change. Tools come from the shared MCP definitions through an explicit allowlist, with no agent-launch tools and no other user or project instructions, hooks, skills or servers. The program starts and counts every call. A backend that cannot meet a role's tools or isolation is skipped with a reason; when none remains, the dream saves progress and exits as partial.

### Rationale

Per-role backends let cheap work run locally and judgement run on a stronger model without code changes. Existing harnesses already execute tools, and explicit lists bound context and cost.

### Alternatives Considered

- **Local first, cloud opt-in**: Rejected by the user as a needless constraint.
- **Local only**: Rejected: caps quality and excludes the harnesses to improve.
- **Cloud by default**: Rejected: makes the cheap path the exception.
- **Direct LM Studio for every role**: Rejected: research and authoring need tools.
- **A new tool-execution loop**: Rejected unless the FOSS assessment (12.2) finds a gap.
- **Full devtools passthrough**: Rejected: adds context and unaccounted delegation.
- **Parallel calls**: Rejected for now (Q4, Q17).

### Consequences

**Positive:**
- One subscription's limit does not stop a dream; role backends can be tuned from statistics.
- Local models can research and author through existing tooling.

**Negative:**
- Each harness and provider pairing must be verified; isolation differs by version.
- LM Studio alone is not enough for tool-using roles.
- Output quality varies, so findings record backend and model.

---

## Decision 3: Treat All Transcripts Alike; Scrub Public Output

**Date**: 2026-11-04
**Status**: accepted

### Context

Transcripts span personal and client-looking projects. agentic-coding and orbit are public. Observations store verbatim quotes.

### Decision

Every transcript is eligible for every backend; there is no per-project routing. The store lives outside every git repository. Changes to a public repository are scanned for transcript text, credentials, tokens and private names, and withheld on a match. The names list is kept only in files outside every repository.

### Rationale

The user chose no project distinction. The same backends already saw these transcripts, so the remaining risk is publication, which the store location and scan address.

### Alternatives Considered

- **Local-only project list**: Rejected by the user as unnecessary.
- **Exclusion list**: Rejected: loses lessons from that work.
- **Scan terms in default-branch configuration**: Rejected: the branch is public.

### Consequences

**Positive:**
- No per-project configuration; no lessons lost.
- Public repositories get only scanned changes.

**Negative:**
- A scan can miss identifiers it does not recognise.
- The default is wrong for a work machine.

---

## Decision 5: A Program Runs the Dream; Models Make Judgements

**Date**: 2026-11-04
**Status**: accepted

### Context

The user wants context separated between an orchestrator and narrow workers. Dreams must lock, dry-run, resume, deduplicate, apply thresholds and gate merges identically every time, with any role movable between backends (Decision 2).

### Decision

The program holds control flow and all state on disk. Model calls are stateless judgements in four roles, each given only its inputs and tools and returning a defined structure. The program plus the reviewer, which sees summaries and never full transcripts, is the "orchestrator". Whether the program is new or adapted FOSS is settled by the design (12.2).

### Rationale

An agent running the loop keeps state in one context that cannot change backend or survive a crash, and its adherence to gates varies. Stateless calls over on-disk state can be retried, resumed and moved; reviewer input stays bounded.

### Alternatives Considered

- **LLM agent as orchestrator**: Rejected: state is tied to one context and backend, and gates depend on instruction following.
- **Program for gates, one agent for the rest**: Rejected: one session would hold transcripts and the cross-session view together.

### Consequences

**Positive:**
- Resume, fallback and adoption behave identically each run.
- Each role can use the cheapest suitable backend; reviewer input stays bounded.

**Negative:**
- The program owns more logic, which needs tests.
- Roles cannot improvise; new steps need program changes, which dreams may propose (7.4).

---

## Decision 6: Portable Learning Foundation with Initial Workspace Integrations

**Date**: 2026-11-05
**Status**: accepted

### Context

The user wants refinement and personal learning to underpin personalised tooling, possibly with a web frontend. Repository commits alone cannot hold preferences and lessons that need no file change, and tying access to the agentic-coding checkout would limit other consumers.

### Decision

Keep the spec and initial integrations in agentic-coding, but make dream operations and retained knowledge independently usable through versioned machine-readable operations and exchange. Repository adoption is an optional output with unchanged checks. Cover personalisation and learning recommendations; defer UI, server and hosting. Build on FOSS: session readers live in apsis, and the design assesses OpenClaw's dreaming code before choosing runtime, language or orchestration (12.2–12.3). Failures follow one retry, then the next backend, with raw errors recorded and vendor limit messages not parsed.

### Rationale

Separating evidence, revisable knowledge and its uses keeps refinement valuable without a repository change. A machine-readable boundary lets another frontend reuse it without choosing a server or framework.

### Alternatives Considered

- **Repository improvements as the only output**: Rejected: loses preferences that are not files.
- **A web product now**: Deferred: adds hosting and accounts not needed for the foundation.
- **An unstructured global profile**: Rejected: loses scope, counter-evidence and history.
- **A new standalone runtime before reuse research**: Rejected: portability does not justify duplicating FOSS.

### Consequences

**Positive:**
- Knowledge works across tools, domains and frontends; evidence, interpretation and corrections stay distinct.

**Negative:**
- Knowledge refinement and exchange need their own acceptance checks.
- Versioned contracts and histories add maintenance.
- A web deployment needs its own requirements.

---

## Decision 7: Tools Acting for the User May Start Dreams

**Date**: 2026-11-05
**Status**: accepted

### Context

Q3 limited starts to the user. The user's scheduled automation acts for them and reads dreaming's knowledge. Continuous refinement with less hands-on work needs more than manual starts.

### Decision

A tool acting for the user may start a dream through 14.1. Dreaming has no scheduler or idle detection, the run record names the requester, and budgets, checks and adoption rules are unchanged.

### Rationale

Scheduling stays in one place; only who may call the existing start operation changes.

### Alternatives Considered

- **Manual starts only**: Rejected: knowledge refreshes only when remembered.
- **Tool-started dreams without auto-apply**: Rejected: two adoption rule sets; checks already gate auto-apply.
- **A scheduler inside dreaming**: Rejected: duplicates the existing scheduler.
- **A cap on start frequency**: Rejected for now: the caller's schedule and per-run budgets bound spend.

### Consequences

**Positive:**
- Knowledge stays current; one scheduler.

**Negative:**
- Unattended dreams may merge and deploy while the user is away.
- Per-run limits do not cap start frequency.
- A scheduled dream can overlap the tool's own work and cause model reloads.
- A tool's start can resume a user-started interrupted dream (1.6, 9.15).

---

## Decision 8: Prompts in Tool-Started Sessions Are Not User Evidence

**Date**: 2026-11-05
**Status**: accepted

### Context

Preferences rest on user statements (13.1, 13.3), but many sessions are started by tools that write the prompts. apsis records the client origin without classifying it.

### Decision

When a session was registered by a tool (14.8), started by a dream, or has a recorded origin configuration marks non-interactive, attribute its prompts to that tool or an unknown author (2.9). They are not user statements, preferences or writing examples, except entries a registration marks as the user's own words; tool results and outcomes remain evidence. Sources with no recorded origin, such as Codex exec streams and unmatched LM Studio sessions, default to non-interactive (Q51).

### Rationale

Counting tool-written prompts would teach dreaming the automation's phrasing as the user's. A configured origin mapping keeps classification out of apsis and user-correctable.

### Alternatives Considered

- **Count every prompt as the user's**: Rejected: corrupts personal knowledge.
- **Rely on registration alone**: Rejected: most headless sessions are unregistered.
- **Treat sessions with no origin as unknown-author**: Rejected: Copilot and Kiro record no origin and are mostly interactive.

### Consequences

**Positive:**
- Preference and style knowledge rests on the user's own words; tool-started sessions still inform tool and workflow lenses.

**Negative:**
- Prompts typed into a headless run are not counted.
- The user's words arrive via a tool only if it marks them (Q53).
- The origin mapping needs updating as harnesses add entry points.

---

## Decision 9: A Thin Runner Executes Each Dream from a SQLite Step Table

**Date**: 2026-11-14
**Status**: accepted

### Context

Dreams must lock, resume, enforce token budgets, isolate each role and fall back between Claude Code, Codex and LM Studio (Decisions 2, 5). The user wants the custom program kept small, rune used where it fits, and the editable instruction set to carry the behaviour. screw is the consumer of dreaming and its gateway is tool-free and incomplete; hosted `orbit run` loads user configuration and `orbit local` drives only Codex on LM Studio. Review found that a rune ledger cannot share a transaction with step results, renumbers positional task IDs when an earlier phase gains a task, and drops `--blocked-by` on `rune add --phase`.

### Decision

A small Python runner (`dream`) keeps every step of a dream in a SQLite `steps` table with stable keys and dependencies; a step's follow-up steps commit in the same transaction as its result. Calls go directly to `claude`, `codex` or LM Studio, and sessions are read through apsis. A rune-format plan is rendered from the table for reading and never read back. The run record, lock and exit codes follow screw's conventions without importing screw.

### Rationale

One transactional store makes resume exact and removes the double-run and renumbering problems. Direct calls are the only way to enforce per-role isolation and token accounting. Rune stays useful as the readable plan format without being the execution state, which also keeps internal execution state out of rune files. Not importing screw keeps dreaming startable before screw is finished and avoids depending on its own consumer.

### Alternatives Considered

- **Rune ledger drives execution**: Reuses ordering and resume - Rejected: two stores to keep consistent, positional IDs, and dropped dependencies.
- **Dream as a screw job**: Inherits lock, limits and profile chain - Rejected: screw's gateway is tool-free and unfinished, and dreaming would depend on its consumer.
- **Orbit executes the steps**: Reuses retry and usage-limit handling - Rejected: hosted orbit loads user instructions and cannot enforce per-role isolation or a token budget.

### Consequences

**Positive:**
- Little orchestration code; resume re-applies stored output instead of calling a model again.
- Backends and roles change through configuration.

**Negative:**
- The runner owns step ordering that rune could have provided.
- screw conventions are copied, so they can drift.

---

## Decision 10: Follow Current Practice Across Implementations; Reuse OpenClaw's Concepts, Not Its Code

**Date**: 2026-11-14
**Status**: accepted

### Context

Requirement 12.2 asks whether OpenClaw's dreaming code can be reused. At `9bf3e79c` (MIT) it is the `memory-core` plugin in TypeScript, bound to OpenClaw's gateway scheduler, SQLite plugin state and `MEMORY.md`. The user also wants the design to follow what makes sense across modern implementations and the literature, not one product. Anthropic's dreaming talk, Letta, Codex memories, Cognee and Mem0 share a shape (out-of-band batch consolidation, staged or reviewable output, provenance), and 2026 papers report that LLM consolidation can degrade memory, turn hedges into facts and be order-sensitive.

### Decision

Re-implement the converging concepts in Python (staged reflection before promotion, recurrence gates, a taint gate on untrusted provenance, operations instead of prose, reviewable and append-only revisions, provenance on every item) and apply the literature's mitigations (raw evidence stays primary, modality is recorded, review order is deterministic, a revision cap, delta edits for instruction changes). No OpenClaw or other product source is copied or vendored. The design's Prior Art section records sources and what each changed.

### Rationale

Reusing OpenClaw's code would carry the gateway and a second runtime for a storage and trigger model dreaming does not share. The concepts map onto requirements 4.5, 11.3 and 13, and the mitigations address reported failure modes at little cost.

### Alternatives Considered

- **Vendor memory-core modules**: Reuses working gates - Rejected: needs a Node runtime and rewriting its storage layer.
- **Run OpenClaw alongside and read its output**: No code in this repo - Rejected: a second always-on system that reads a different session corpus.
- **Adopt a memory framework (Mem0, Graphiti, Letta)**: Reuses extraction and retrieval - Rejected: vector or graph stores and server processes for a single user's scale, and Mem0 itself dropped LLM reconcile as lossy.

### Consequences

**Positive:**
- One runtime; no licence attribution duty; the design owns its data model.
- Known failure modes of LLM consolidation are designed against.

**Negative:**
- The gates and mitigations are re-implemented and tested here.
- Upstream improvements need manual review to benefit from.
- Sources are 2026 preprints and product documentation, some unreviewed; findings are treated as direction, not proof (see Risks and Assumptions).

---

## Decision 11: Versioned Installs Keep a Run on Its Starting Version

**Date**: 2026-11-14
**Status**: accepted

### Context

Requirement 7.3 requires a run, including a resumed part, to finish on the program version it started with, while adoption may deploy new dreaming code mid-run.

### Decision

Each deploy installs into `~/.dreaming/versions/<revision>/` and repoints `current`. The launcher uses `current` for new runs and the recorded revision for resumes. A version is removed only when no unfinished run records it.

### Rationale

A running process is unaffected by a new install, but a resume would load whatever is installed; selecting the interpreter by recorded revision makes the guarantee hold for both.

### Alternatives Considered

- **Editable install from the checkout**: Simplest - Rejected: adoption changes code under a running or resumed run.
- **Refuse to resume on version mismatch**: Simple - Rejected: strands interrupted runs and defeats 1.6 resume.

### Consequences

**Positive:**
- Resume behaves the same regardless of intervening deploys.

**Negative:**
- Disk use per version and a launcher to maintain.
- Migrations are additive and run only when no unfinished run exists, so a newer command can refuse (`migration_blocked`) until an interrupted run is resumed or discarded.

---

## Decision 12: Instruction Changes Must Pass a Session Replay Before Auto-Apply

**Date**: 2026-11-14
**Status**: accepted

### Context

Decision 1 auto-applies changes when lint and test pass on the merged result. For skills, rules, lenses and role prompts these checks do not show behaviour improved. Research on self-modifying agents (Darwin Gödel Machine, GEPA) validates changes against evaluations, and a 2026 paper reports LLM-consolidated memory can leave agents worse than none.

### Decision

A change to a skill, rule, lens or role prompt that cites session observations is run once per authoring, with and without the change, on up to a configured number of past sessions showing its pattern; a session counts only if the problem recurs without the change, and an unclear judgement counts as recurrence. Lens and role-prompt changes re-run the role on stored inputs instead. If the problem recurs with the change in more than a configured share, too few sessions are replayable, or the budget cannot cover it, the change is held and its whole repository branch stays unmerged (9.16, 9.17). Replay counts against the run budget and is reported as association.

### Rationale

Auto-apply was accepted on the basis that checks gate what goes live; for instructions the checks are blind to the thing that matters. A replay is the cheapest available behavioural test using material the store already holds.

### Alternatives Considered

- **Single replay run with the change only**: Cheaper - Rejected: a change that does nothing passes.
- **Report the replay but do not gate**: Cheaper to trust - Rejected by the user: a failed replay would not stop a change.
- **Replay every change, including code**: Safest - Rejected: code changes have no recorded prompt to replay and would be blocked.
- **Turn auto-apply off for instruction repositories**: Simple - Rejected: removes the low-friction path Decision 1 chose.

### Consequences

**Positive:**
- Instruction changes face a behavioural check before going live.

**Negative:**
- Replay roughly doubles cost, and an LLM judgement of recurrence is not proof.
- One held change keeps the whole repository branch unmerged, because the merge is one commit.
- Patterns with few sessions cannot be replayed, so their changes wait on the branch.

---

## Decision 13: Forgetting a Session Excludes It and Keeps Its Data

**Date**: 2026-11-14
**Status**: accepted

### Context

OpenClaw (`memory forget`) and Codex (per-chat controls) let a user remove a session from consolidation. Dreaming's store keeps redacted evidence and per-session counts after transcripts disappear (6.6, 13.7), and dismissal acts on knowledge items or individual evidence only.

### Decision

Forgetting marks a session excluded: it is not selected, re-read or registered again, its observations stop counting as support or ranking signal, knowledge it supported is marked pending reassessment, and it is omitted from retrieval evidence and listed in the report. Its stored data remains, and restoring the session reverses the exclusion (2.11).

### Rationale

The user chose an easily reversible exclusion over deletion. Reassessment keeps dependent knowledge visible as doubtful without removing it silently.

### Alternatives Considered

- **Delete the data and keep a tombstone**: Removes private quotes - Rejected by the user in favour of reversibility.
- **Delete and dismiss knowledge supported only by that session**: Strongest - Rejected: a single forgotten session could silently remove knowledge the user still wants.

### Consequences

**Positive:**
- Reversible; no accidental loss of evidence or knowledge.

**Negative:**
- Quotes from a forgotten session stay in the private store, so this does not erase them; deleting stored data is a non-goal, and the user must edit or delete the store to erase it.

---

## Decision 14: Currency Review Is a Branch-Only, Source-Cited Dream

**Date**: 2026-11-14
**Status**: accepted

### Context

Skills, rules and agentic processes go stale as practice changes, and nothing in recorded sessions shows it. The user wants periodic review against current literature and implementations. A currency finding has no session behind it, so the session threshold (4.5), pattern identifiers (6.5) and the replay gate (Decision 12) do not apply, and fetched web content is untrusted input to instruction changes.

### Decision

A currency-review dream starts only on explicit request (including from a tool, Decision 7), reads no sessions, and searches configured targets within the run budget and a source limit. A candidate needs a configured number of independent sources and verification that each cited claim supports it; fetched content is data, never instructions. Resulting changes are committed to the dream branch with citations and never auto-applied; other findings are retained as currency notes. Each finding has a persistent identifier per target and claim so a rejected finding is not proposed again unless a source changed (16).

### Rationale

Without recorded sessions there is nothing to replay and no user evidence, so a human merge is the only available check. Requiring independent sources, verbatim verification of relied-on passages and delimited data fields limits a single page from steering instructions.

### Alternatives Considered

- **Auto-apply when lint and tests pass**: Same path as other changes - Rejected: fetched content would steer instructions without a human check.
- **Learning recommendations only**: Never touches a repository - Rejected: leaves every update manual even when a source is clear.
- **A single source suffices**: Cheaper - Rejected: a single fetched page could steer instructions.

### Consequences

**Positive:**
- Instructions can be refreshed against current practice without weakening the replay gate.

**Negative:**
- Every currency change needs the user to merge it.
- The independent-source threshold and web search quality limit what is found.

---

## Decision 15: Changes That Received Fetched Web or PR Content Stay on the Branch

**Date**: 2026-11-14
**Status**: accepted

### Context

Researchers can fetch web pages and read pull-request review threads (4.6), and the author sees what they return. The replay gate (Decision 12) checks whether a problem recurs, not what a change added, and changes citing only file findings skip replay. Fetched text from third parties could therefore steer an instruction change that auto-applies.

### Decision

A change whose research or authoring received fetched web or pull-request content is never auto-applied; it is committed to the branch and reported with its sources, as currency changes are (9.19). Changes based only on sessions and local files still auto-apply after their checks and, where applicable, replay.

### Rationale

Third-party text is the one input an attacker can write without access to the user's machine, so a human merge is the proportionate check.

### Alternatives Considered

- **Web research only in currency dreams**: Narrows the input surface - Rejected: PR threads remain third-party text and requirement 4.6 would narrow.
- **Allow auto-apply after replay**: Lowest friction - Rejected: replay cannot see injected text.

### Consequences

**Positive:**
- No third-party text reaches an auto-applied change.

**Negative:**
- Changes that used web research wait for the user's merge.
- A single such change keeps its repository branch unmerged (9.17).

---

## Decision 16: Own-Budget Stop Is Success; Provider Usage Limits Are Waited Out

**Date**: 2026-11-14
**Status**: accepted

### Context

The design exited 5 for both budget exhaustion and a role with no backend left, following screw. Reaching a limit the user configured is the intended end of a bounded dream, not a degraded one. Provider usage limits (Claude "resets at …", Codex "try again at …") are different: they come with a reset time, and orbit already sleeps until it instead of failing. Treating them as transport failures would mark a backend unavailable for the run over a pause of minutes or hours.

### Decision

Reaching the dream's own token, session or wall-clock limit ends the run as `budget_exhausted` with exit 0. A usage limit with a reset time within `max_usage_wait` makes the run sleep until then and repeat the call, without counting a retry or failure and without counting the wait toward wall-clock. A configuration option `fallback_on_usage_limit` (default off) makes a usage limit move the call to the role's next backend instead; while waiting, the CLI shows the option with the context size of the current and next backend and, where reported, their model release dates. A usage limit with no usable reset time or a longer wait is a transport failure; with no backend left the run ends `partial` with exit 5.

### Rationale

Exit 5 then means "stopped short of the plan for a reason outside the budget", which is what a caller (screw, a scheduler) needs to act on. Orbit's behaviour is reused so the two tools treat provider limits alike.

### Alternatives Considered

- **Never sleep; stop at a usage limit with exit 5 and resume later**: Simple, no long-lived sleeping process - Rejected: each limit needs a manual or caller-driven resume although the reset time is known.
- **Keep budget exhaustion at exit 5**: Matches screw - Rejected: a normal bounded run would look like a failure to callers.
- **Fall back to the next backend by default**: Keeps the run moving and brings a different model's view - Rejected as the default: a smaller context may not suit larger tasks. Crossing models and harnesses is wanted, so it stays an advertised option.
- **Count the wait as wall-clock**: Simpler accounting - Rejected: an overnight reset would exhaust the time limit with no work done (1.2 counts only running time).

### Consequences

**Positive:**
- A bounded dream that finishes its budget reports success.
- Usage limits no longer disable a backend for the rest of the run.

**Negative:**
- The detached process may sleep for hours while holding the lock, so a second start exits 4 until it ends.
- Exit codes differ from screw for budget exhaustion.
- Reset-time parsing depends on message formats that vary by harness version; unrecognised forms fall back to transport failure.

---

## Decision 17: Role-Prompt Changes Without Stored Inputs Are Held

**Date**: 2026-11-15
**Status**: accepted

### Context

Requirement 9.16 re-runs the role a lens or role-prompt change belongs to on the stored inputs of its earlier calls. The store keeps what a Read step needs to rebuild its reader prompts: the session reference in `steps.input_ref` and the session itself through apsis. It keeps no other role's prompt. The `calls` table holds raw outputs and the sessions an input contained, and reviewer, researcher and author prompts are built from store state that later runs change.

### Decision

A change to the lenses or the reader's prompt re-runs the reader on the latest finished Read step of each replayed session, under that step's run scope, once with the run's instructions and once with the changed files. A change to any other role prompt (reviewer, researcher, author, replayer or judge) is held because that role's earlier inputs are not kept, and its branch stays unmerged (9.17).

### Rationale

Holding treats an unreplayable change the way 9.17 treats one with too few replayable sessions. Rebuilding another role's input from the current store would replay inputs its calls never saw, so a pass would say nothing about the change.

### Alternatives Considered

- **Store every call's prompt**: Any role could be re-run - Rejected for now: needs a schema change and roughly doubles the store, since reader prompts carry session text; it remains possible later as an additive migration.
- **Rebuild other roles' inputs from the current store**: No new storage - Rejected: the reviewer's and author's inputs depend on state that later runs changed, so the replay would test different inputs.
- **Let checks alone gate other role prompts**: Lowest friction - Rejected: Decision 12 gates instruction changes on behaviour because lint and test cannot see a prompt's effect.

### Consequences

**Positive:**
- No change to a prompt that cannot be replayed is auto-applied.
- No schema change.

**Negative:**
- Changes to the reviewer, researcher, author, replayer and judge prompts always wait for the user's merge, with the reason in the report.
- Only the reader's prompt and the lenses benefit from replay among dreaming's own instructions.

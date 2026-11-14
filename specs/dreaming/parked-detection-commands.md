# Parked: detection commands (post-MVP)

Parked 2026-11-14. Requirement 17, Decision 16, task 19 and the supporting design text were added to
specs/dreaming (with matching contract fields in specs/dry), then removed before the MVP cut: DRY adds
nothing the MVP needs. Dreaming cannot act on a duplication finding (the resolution skills are interactive,
so 9.3 defers them), file findings get no before/after rate or replay, and `/dedupe-scan` already lists
clusters on demand.

To reinstate: amend requirements (17), re-add the design pieces, the decision entry and the task, then
apply the dry changes below. Decision 16 was `proposed` and never approved. Task and requirement numbers
below are those at the time (the task was inserted as 19, shifting later tasks by one).

The first dream after the MVP is the natural moment to revisit it.

## Removed: dreaming: Requirement 17
#### 17. Detection Commands

**User Story:** As the user, I want a lens to use a deterministic detector I already have, so that findings such as code duplicated across repositories are found by a tool, kept between dreams and reported without a model guessing at them.

**Acceptance Criteria:**

1. <a name="17.1"></a>WHERE a lens names a detection command configured for a set of repositories, the system SHALL run that command in a scan step before review, taking the command from the run snapshot ([7.3](#7.3)), within a configured timeout, and SHALL record its exit status and duration in the run record ([7.5](#7.5)).  
2. <a name="17.2"></a>The system SHALL store each item the command reports as a file finding ([6.1](#6.1)) with the repository, the revision the system read from the repository when the command ran, the path, a location (the whole file when the command gives none), the producing lens and the command's own item identifier; reporting the same item again under an unchanged revision SHALL NOT create a duplicate, and the item's pattern SHALL keep its persistent identifier across dreams ([6.5](#6.5), [13.9](#13.9)).  
3. <a name="17.3"></a>A detection finding SHALL be a candidate on that evidence alone ([4.5](#4.5)), SHALL NOT be replayed ([9.16](#9.16)) and SHALL NOT count toward knowledge about the user ([13.6](#13.6)). WHEN its resolution requires an interactive skill, THEN the step SHALL be deferred and reported with the finding's evidence and the skill to run ([9.3](#9.3)).  
4. <a name="17.4"></a>IF the command is missing, exits non-zero, times out or prints output that does not match its declared schema version, THEN the system SHALL record the reason, list it in the report and continue; a missing detector SHALL NOT block knowledge refinement ([14.4](#14.4)).  
5. <a name="17.5"></a>The first configured detection command SHALL be agentic-coding's `dedupe_scan.py --json` (specs/dry Requirements 1.8 and 1.9), read through its `schema_version` field; the system SHALL hold no copy of its detection logic.  

## Removed: dreaming: Decision 16
#### Decision 16: Duplication Detection Comes from a Configured Command; Resolution Stays with the User

**Date**: 2026-11-14
**Status**: proposed

#### Context

`specs/dry` provides `dedupe_scan.py`, a deterministic detector for code duplicated across repositories, and four interactive skills that resolve what it finds. Dreaming accepts file findings without a session behind them (4.5, 6.1) but has no step that produces any. The dry spec requires every resolution to end in a plan the user approves; Decision 1 here auto-applies changes whose checks pass.

#### Decision

Add detection commands (section 17): a lens may name a configured command, a Scan step runs it without a model and stores each reported item as a file finding. The first command is `dedupe_scan.py --json`. Dreaming holds none of its logic and does not run `shared-extract` or `repo-consolidate`; a resolution that needs them is deferred under 9.3 with the evidence and the skill to run.

#### Rationale

A deterministic detector is cheaper and more reproducible than a model reading repositories, and dry already owns it. Deferral reuses 9.3, so dry's approval gates stay with the user and no new adoption rule or never-merge category is needed. Every cluster spans two or more repositories, so an authored change would be a linked group (9.10) with no replay (file findings are not replayed), which is the weakest case for auto-apply.

#### Alternatives Considered

- **Merge the two specs**: One document - Rejected: dry has no runtime and ships as skills and one script, dreaming is a 31-task program in its own repository, and merging couples their release order.
- **Reimplement detection inside dreaming**: No cross-repo dependency - Rejected: duplicates the code dry exists to remove (12.1) and violates 14.4 only if it imports, so it would have to copy.
- **A model reads repositories to find duplication**: No detector to install - Rejected: not reproducible, costs tokens on every dream, and misses near-identical files a hash finds.
- **Branch-only changes for cross-repository findings**: Authors the consolidation anyway - Rejected: needs a new never-merge category in 9.7 where 9.3 already gives the same outcome.

#### Consequences

**Positive:**
- Findings persist across dreams under one pattern identifier and appear in the report.
- No new adoption rule; dry's approval gates are unchanged.

**Negative:**
- Dreaming reports duplication but does not fix it; the user runs the dry skills.
- File findings have no before/after rate (design: effect measurement), so the effect of a consolidation is checked by running the scan again.
- The report schema is a contract between two specs; a change to either needs the other reviewed.
## Removed: dreaming: design, Detection commands
#### Detection commands (`detect.py`, 17.x)

A lens may carry `detector = "<name>"`; `[detectors.<name>]` in the config gives `command`, `repos`, `timeout_seconds` and the `schema_version` it accepts. The Scan step runs the command from the snapshot with no model call, so no role isolation or token budget applies, and records exit status and duration on the run. The program reads each repository's `git rev-parse HEAD` for the revision, then writes one `file_findings` row per reported item inside the step's result transaction. `pattern_key` is `<detector>:<item id>`, so the deterministic clustering and 6.5 give a returning item its existing pattern id. An item's `location` is the whole file unless the command reports a line range.

The first detector is `dedupe_scan.py --json` from agentic-coding's `specs/dry`: one item per cluster, its cluster id as the item id, members as path and repository. Its shape field (extraction or consolidation) is a heuristic that dry's skill confirms with the user, so dreaming records it as `unconfirmed` and shows it so. A candidate from this lens is authored only as a report entry: the resolution skills (`shared-extract`, `repo-consolidate`) are interactive and are deferred under 9.3 with the evidence and the skill name, and every cluster spans repositories, so any authored change would be a linked group under 9.10. A non-zero exit, timeout or unknown `schema_version` is recorded and listed in the report; the dream continues (17.4). Detection logic is not copied into this repository (14.4, 17.5).

## Removed: dreaming: task 19
- [ ] 19. Detection commands: Scan step and dedupe lens 
  - Blocked-by: w1na5d2 (SQLite store and migrations), w1na5d5 (Configuration, lens loading and run snapshot), w1na5dl (Steps, calls, budget, lock and resume), w1na5e3 (Roles, schemas and the instruction set)
  - Stream: 1
  - Requirements: [17.1](requirements.md#17.1), [17.2](requirements.md#17.2), [17.3](requirements.md#17.3), [17.4](requirements.md#17.4), [17.5](requirements.md#17.5), [4.5](requirements.md#4.5), [6.1](requirements.md#6.1), [6.5](requirements.md#6.5), [9.3](requirements.md#9.3), [14.4](requirements.md#14.4)
  - [ ] 19.1. Write tests for the Scan step and detection findings 
    - Fake detector command: findings stored with the revision read from the repository and no duplicates on rerun; a returning item keeps its pattern id; non-zero exit, timeout and unknown schema_version recorded and the dream continues; a detection finding is never replayed or counted as user evidence; an interactive resolution deferred with the skill name
    - Blocked-by: w1na5d2 (SQLite store and migrations), w1na5d5 (Configuration, lens loading and run snapshot), w1na5dl (Steps, calls, budget, lock and resume), w1na5e3 (Roles, schemas and the instruction set)
    - Stream: 1
    - Requirements: [17.1](requirements.md#17.1), [17.2](requirements.md#17.2), [17.3](requirements.md#17.3), [17.4](requirements.md#17.4)
  - [ ] 19.2. Implement detect.py, the Scan step and the duplicated-code lens 
    - detect.py runs [detectors.<name>] commands from the snapshot; first detector is dedupe_scan.py --json from specs/dry (task dry01c installed, see prerequisites.md); pattern_key is <detector>:<item id>; shape recorded as unconfirmed; add the duplicated-code-across-repositories lens to instructions/lenses.toml; no detection logic copied
    - Blocked-by: w1na5fm (Write tests for the Scan step and detection findings)
    - Stream: 1
    - Requirements: [17.1](requirements.md#17.1), [17.2](requirements.md#17.2), [17.3](requirements.md#17.3), [17.4](requirements.md#17.4), [17.5](requirements.md#17.5)

## Removed: dry: requirements 1.8 and 1.9
8. <a name="1.8"></a>The detection script SHALL stamp its JSON report with a `schema_version` and give each cluster a stable `id`, derived from the cluster's lowest-sorted member repo and path, so that other tools can consume the report and recognise a cluster across runs
9. <a name="1.9"></a>The detection script SHALL accept a `--json` flag that prints only the JSON report to stdout, with warnings on stderr

## Removed: dry: Consumers
## Removed: Consumers

`specs/dreaming` (Requirement 17) runs `dedupe_scan.py --json` as a configured detection command
and stores each cluster as a file finding. The report schema and cluster id are therefore a
contract: a change to either needs `specs/dreaming` Requirement 17 reviewed. Dreaming never runs
`shared-extract` or `repo-consolidate`; it defers a resolution that needs them and names the
skill to run, so the approval gates in Requirements 2.2 and 3.3 stay with the user
(Decision Q1 in `decision_log.md`).

## Removed: dry: Quick decision Q1
## Removed: Quick Decisions

| ID | Date | Decision | Rationale |
|----|------|----------|-----------|
| Q1 | 2026-11-14 | The `dedupe_scan.py` JSON report is a versioned contract consumed by `specs/dreaming` (Requirement 17); dreaming defers resolutions to the interactive skills here instead of running them | Keeps the "no auto-merge, plan approved first" rule with the user; see `specs/dreaming` Decision 16 |


## Removed: small edits to reverse-apply

specs/dreaming/requirements.md, Status: appended "Section 17 was added 2026-11-14 and awaits approval (Decision 16)."

specs/dreaming/design.md:
- Layout list: `    detect.py            # configured detection commands to file findings (17.x)` after the `adopt.py scan.py` line.
- Steps phases: "Read (one step per session batch), Scan (one step per configured detection command, 17.x), Review, ..." and "Review depends on all Read and Scan steps."
- Data Models `file_findings` row: added columns `detector, detector item id`.
- Testing Strategy: a "Detection (17.x)" row before the Adoption row.

specs/dreaming/prerequisites.md: "Before task 19 (detection commands) is exercised against real repositories: `dedupe_scan.py` from `specs/dry` task 1.2 (id dry01c) is installed in agentic-coding `scripts/`, and a `[detectors.dedupe]` entry in `~/.dreaming/config.toml` gives its command, the repositories to scan and the `schema_version` it accepts (17.5). Tests use a fake detector and do not need it." Task references after 18 were shifted by one (adoption checks 24 to 25, end-to-end 31 to 32).

specs/dreaming/tasks.md: task 19 (subtasks 19.1 tests, 19.2 implementation) blocked by the store, config, steps and roles tasks; the Reviewer phase task and its test subtask gained a Blocked-by on it.

specs/dry/design.md:
- Data model JSON: `{ "schema_version": 1, "clusters": [ {` and `"id": "c-3f9a1b72"` (hash of the lowest-sorted member (repo, path)); the original was `{ "clusters": [ {` with `"id": "pdf-pipeline"`.
- Detection algorithm step 5: added "`--json` prints only the report to stdout, with warnings on stderr (Req 1.9). The report carries `schema_version`, and each cluster id is a short hash of its lowest-sorted member repo and path (Req 1.8); the id changes only if that member disappears, which a consumer treats as a new cluster."
- Traceability row "1.1–1.9" (was "1.1–1.7").

specs/dry/tasks.md: dry01a gained "Report carries schema_version; cluster id is a short hash of the lowest-sorted member (repo, path); --json prints only the report to stdout, warnings to stderr (consumed by specs/dreaming Requirement 17)" and requirements 1.8, 1.9; dry01b gained "Contract: two runs over the same fixtures give identical cluster ids; the report has schema_version; --json stdout parses as the report alone" and requirements 1.8, 1.9.

# Requirements: dry

## Introduction

The `platform/` family has grown by copy-paste. Three duplication clusters are confirmed in
the code (evidence in `design.md`):

- **A — two apps, one job:** `sdd-ui` and `spadre` are both SvelteKit + Svelte 5 apps that
  render markdown spec documents into line-anchored blocks, extract headings for an outline,
  pull out mermaid fences, sanitize HTML, and rewrite image refs. They diverged on the
  markdown stack (`marked`+DOMPurify vs `remark/rehype`+shiki) and have drifted independently.
- **B — one pipeline, three copies:** `brandme`, `toes`, and `invoicer` each carry a near-identical
  branded-PDF pipeline (`config`, `models`, `branding`, `formatting`, `md_renderer`,
  `pdf_renderer`, `shadow`). `md_renderer.py` is byte-for-byte identical between brandme and
  toes except one import line.
- **C — process/docs:** the SDD spec layout and spadre's stack-agnostic `PROCESS.md` are
  re-copied into every repo.

This spec defines a **DRY skills base** in `agentic-coding` that (1) detects duplication across
a set of repos, (2) resolves a cluster by extracting shared code or consolidating overlapping
repos, and (3) prevents recurrence by routing new work to **spawn from a parent repo** instead
of copy-pasting. The skills are the repeatable process; the two known clusters (A and B) are
the reference cases that prove it works.

## Definitions

- **Cluster**: a set of files in two or more repos that implement the same capability and are
  identical or near-identical. A cluster is **extraction-shaped** when the shared part is a
  library/module that each repo could import, and **consolidation-shaped** when two repos are
  whole applications doing the same job.
- **Parent repo**: a repo that owns a reusable capability (a PDF pipeline, a site engine) and
  exposes a **spawn mechanism** — the means by which a new consumer is created with that
  capability already wired in. `siteme` is already a parent for sites (`make new-site`,
  `SITE_DIR`).
- **Consumer repo**: a repo spawned from a parent that holds only its own content/config and
  references the parent for shared code rather than copying it.
- **Spawn**: creating a consumer from a parent via the parent's spawn mechanism, as opposed to
  cloning a sibling and editing it.

## Non-Goals

- **Not a one-time cleanup.** The skills are the deliverable; running them against clusters A
  and B is the *first application*, tracked in `tasks.md`, not a separate project.
- **No forced monorepo.** Repos stay independent git repos; sharing is via a parent reference
  (git dependency, workspace, or path), not by merging everything into one repo.
- **No change to spadre's deployment.** Cluster A resolution is a plan + migration; it does not
  alter the Azure/OpenTofu stack in this spec.
- **No new runtime.** The skills are LLM-instruction markdown, consistent with every other skill
  in the tree. Any detection helper is a small script the skill invokes, not a service.
- **No auto-merge.** Every resolution ends in a plan the user approves; the skills edit files
  only after approval, and committing remains a user action.

## Requirements

### 1. Detection (`dedupe-scan`)

**User Story:** As a maintainer, I want to point one skill at a set of repos and get back the
duplication clusters with evidence, so that I can see what is repeated before deciding how to
resolve it.

**Acceptance Criteria:**

1. <a name="1.1"></a>WHEN invoked with a list of repo paths (or a parent directory to scan),
   the skill SHALL enumerate source files across all of them, excluding generated and vendored
   content (`node_modules`, `.git`, build output, lockfiles, caches)
2. <a name="1.2"></a>The skill SHALL group files into clusters by content: exact matches via a normalized content
   hash, and near-matches via a similarity measure that is insensitive to import-path and
   whitespace differences (so `from .formatting` vs `from lib.formatting` still clusters)
3. <a name="1.3"></a>FOR each cluster the skill SHALL report: the member files with their repos,
   a similarity metric, and a sample diff as evidence
4. <a name="1.4"></a>The skill SHALL classify each cluster as **extraction-shaped** or **consolidation-shaped**
   and name the recommended resolution skill
5. <a name="1.5"></a>WHEN run against the known cluster B repos (`brandme`, `toes`, `invoicer`), the skill SHALL
   report a single PDF-pipeline cluster containing at least `md_renderer.py` and
   `pdf_renderer.py` from all three, classified extraction-shaped
6. <a name="1.6"></a>WHEN run against `sdd-ui` and `spadre`, the skill SHALL report a markdown-rendering cluster
   (block/heading/mermaid handling) classified consolidation-shaped
7. <a name="1.7"></a>The skill SHALL write no files; its only output is the report presented to the user

### 2. Shared extraction (`shared-extract`)

**User Story:** As a maintainer, I want to hand an extraction-shaped cluster to one skill and
have the shared code pulled into a single package with each repo refactored to import it, so
that the pipeline is maintained in one place.

**Acceptance Criteria:**

1. <a name="2.1"></a>WHEN invoked with an extraction-shaped cluster, the skill SHALL identify
   the exact shared module set and the per-repo deltas (the parts that legitimately differ)
2. <a name="2.2"></a>The skill SHALL propose a single home for the shared code — an existing
   parent repo or a new package — and SHALL present that proposal for approval before writing
3. <a name="2.3"></a>WHEN approved, the skill SHALL create or update the shared module and
   refactor each member repo to import it, preserving each repo's deltas
4. <a name="2.4"></a>The skill SHALL verify the change by running each affected repo's existing tests/build and
   report the result; a failing verification SHALL leave the change marked incomplete
5. <a name="2.5"></a>APPLIED to cluster B, the shared set SHALL be
   `{config, models, branding, formatting, md_renderer, pdf_renderer, shadow}` with brandme as
   the home (it is the superset), and `toes`/`invoicer` retaining only their doctype templates
   and customer logic

### 3. Consolidation (`repo-consolidate`)

**User Story:** As a maintainer, I want to hand two overlapping repos to one skill and get back
a consolidation plan — which is canonical, what gets absorbed, what retires, and the migration
steps — so that one job is done by one app.

**Acceptance Criteria:**

1. <a name="3.1"></a>WHEN invoked with two repos, the skill SHALL produce a capability map:
   what each repo does, the overlap, and each repo's unique capabilities
2. <a name="3.2"></a>The skill SHALL recommend a canonical repo and classify every capability as **absorb** (port
   into the canonical), **already-present**, or **retire**
3. <a name="3.3"></a>The skill SHALL emit an ordered migration plan with a verification step per port, and SHALL
   present it for approval before any source edit
4. <a name="3.4"></a>APPLIED to cluster A,
   the plan SHALL name `spadre` canonical (it is the superset and the intended generalisation)
   and classify sdd-ui's agent-bridge, Orbit panel, and dispatch sheet as **absorb**

### 4. Spawn-from-parent guard (`simplicity-guard`)

**User Story:** As a maintainer starting new work, I want to be told whether an existing parent
repo already owns the capability I need, so that I spawn a consumer instead of copy-pasting a
sibling.

**Acceptance Criteria:**

1. <a name="4.1"></a>WHEN invoked with a description of new work (or a draft repo), the skill
   SHALL scan known parent repos for an owned capability that matches
2. <a name="4.2"></a>IF a matching parent exists, THEN the skill SHALL recommend spawning from it and name the
   spawn mechanism; OTHERWISE it SHALL say the capability is genuinely new
3. <a name="4.3"></a>The skill SHALL maintain (or read) a registry of parent repos and the capabilities they own,
   so the check is repeatable without re-deriving it

### 5. Parent-repo model & spawn mechanism

**User Story:** As a maintainer, I want each capability-owning repo to expose one spawn
mechanism, so that "make a new thing of this kind" is a command, not a copy.

**Acceptance Criteria:**

1. <a name="5.1"></a>A parent repo SHALL expose a spawn mechanism that creates a consumer with
   the shared capability already wired in (the pattern `siteme` uses: `make new-site NAME=…`)
2. <a name="5.2"></a>`brandme` SHALL be designated the parent for the branded-PDF capability,
   and its spawn mechanism SHALL produce a consumer that imports the shared pipeline rather than
   carrying copies of it
3. <a name="5.3"></a>Each parent SHALL declare the capabilities it
   owns in a machine-readable form that `simplicity-guard` (Req 4) reads

### 6. Skills-base conventions

**User Story:** As a maintainer, I want the DRY skills to live and behave like every other skill
in `agentic-coding`, so the base is consistent and spawnable from there.

**Acceptance Criteria:**

1. <a name="6.1"></a>Each skill SHALL live at `claude/skills/<name>/SKILL.md` in
   `agentic-coding`, with YAML frontmatter (`name`, `description`) and a phased markdown
   workflow, matching the existing skills
2. <a name="6.2"></a>Each skill SHALL gate
   state-changing steps on explicit user approval, consistent with the starwave skills
3. <a name="6.3"></a>The skill registry in the `agentic-coding` README SHALL list the new DRY
   skills

# Decision Log: dry

## Decision 1: Skills base lives in agentic-coding

**Date**: 2026-11-14
**Status**: accepted

### Context

The DRY skills need a home. They could live in each affected repo, in a new dedicated repo, or
in the existing harness that already owns every other skill.

### Decision

All four skills live in `agentic-coding/claude/skills/<name>/SKILL.md`, alongside the existing
skills. The detection helper (`scripts/dedupe_scan.py`) and parent registry
(`scripts/parents.json`) live in `agentic-coding/scripts/`.

### Rationale

The user directed that the skills base use `agentic-coding` because "things should spawn from
there anyway." It is already the single source for the skill tree and the sync/align toolchain
that seeds skills into repos, so a DRY skill added there is automatically available everywhere.

### Alternatives Considered

- **Per-repo skills**: Rejected — would itself be duplication, the very thing this spec removes
- **New dedicated repo**: Rejected — a second skills home splits the tree and the sync tooling

### Consequences

**Positive:** One skills source of truth; DRY skills are spawnable into any repo via the existing
align/sync toolchain.

**Negative:** `agentic-coding` gains a small executable (`dedupe_scan.py`) and a registry file it
did not previously need.

---

## Decision 2: A four-skill pipeline (scan → resolve → guard)

**Date**: 2026-11-14
**Status**: accepted

### Context

Duplication has two shapes (extraction vs consolidation) and a prevention angle. The skill
decomposition could be one big skill, two (detect + fix), or a finer pipeline.

### Decision

Four skills: `dedupe-scan` (detect), `shared-extract` (resolve extraction-shaped),
`repo-consolidate` (resolve consolidation-shaped), `simplicity-guard` (prevent by routing new work
to a parent).

### Rationale

Detection is read-only and reusable on its own, so it earns a standalone skill. The two cluster
shapes need genuinely different resolution (import-a-package vs port-capabilities-and-retire), so
they are separate skills rather than one with a branch. Prevention is a different trigger (new
work, not existing dupes) and a different input (intent, not files), so it is its own skill.

### Alternatives Considered

- **One `dedupe` skill that branches**: Rejected — conflates a read-only scan with two very
  different write workflows; harder to invoke just the scan
- **Two skills (detect + fix)**: Rejected — forces one "fix" skill to handle both extraction and
  consolidation, which have different approval surfaces

### Consequences

**Positive:** Each skill has one job and one trigger; the scan can run repeatedly as a health
check without implying any change.

**Negative:** Four skills to keep in sync with the registry and README; more surface than a
single skill.

---

## Decision 3: Parent-repo model, with brandme and siteme as the first parents

**Date**: 2026-11-14
**Status**: accepted

### Context

The user framed the goal as "spawn from a parent repo (perhaps brandme or siteme)." The model
could be a shared library each repo imports, a monorepo workspace, or a parent that spawns
consumers.

### Decision

Generalize the pattern `siteme` already uses: a **parent** owns a capability and exposes one
spawn mechanism; a **consumer** holds only its own content/config and references the parent.
`siteme` is the site parent (already working: `make new-site`, `SITE_DIR`). `brandme` becomes the
branded-PDF parent (new spawner: `make new-docapp`). A registry (`scripts/parents.json`) records
each parent and its capabilities so `simplicity-guard` can match against it.

### Rationale

`siteme` proves the model works in this family — consumer repos already hold just YAML and point
at the engine. Reusing a proven pattern is lower-risk than inventing a new sharing mechanism, and
it matches the user's "spawn from a parent" framing exactly. brandme is the natural PDF parent
because it is the superset of the cluster B pipeline (it has CV, QR, icons, animate that toes and
invoicer lack).

### Alternatives Considered

- **Shared library only (no spawn)**: Rejected — importing a package still leaves each new repo
  hand-wiring the consumer; a spawner makes "new thing of this kind" one command
- **Monorepo workspace**: Rejected — the family is deliberately independent git repos; a monorepo
  changes deployment and ownership for every repo

### Consequences

**Positive:** New doc/site work is a spawn, not a copy; the registry makes "is there a parent for
X?" answerable without re-deriving it.

**Negative:** Parents must maintain their spawn mechanism and registry entry; a stale spawner
silently produces out-of-date consumers.

---

## Decision 4: Scope — design for any repo set; first application is the named repos + invoicer

**Date**: 2026-11-14
**Status**: accepted (baseline)

### Context

The user gave five READMEs but `platform/` holds ~20 repos. The scan could be scoped to the five,
the whole family, or designed to take any set.

### Decision

`dedupe-scan` takes an explicit repo list or a parent directory, so it works on any set. The
baseline's first application targets the five named repos plus `invoicer` (the confirmed cluster B
sibling). A full-family scan is a later invocation of the same skill, not part of this baseline.

### Rationale

The two confirmed clusters (A and B) are enough to prove the skills work end-to-end. Scoping the
first run to them keeps the baseline small and reviewable; the skill's design already handles the
larger set, so widening scope later is just a bigger input, not new work.

### Alternatives Considered

- **Full-family scan now**: Rejected for the baseline — larger surface, more clusters to triage
  before the skills are even built; better as the first real invocation of `dedupe-scan`
- **Five repos only, hard-coded**: Rejected — the skill should not bake in a repo list

### Consequences

**Positive:** Baseline stays focused; the proof cases are exactly the confirmed clusters.

**Negative:** Duplication in other `platform/` repos (clocker, formfiller, siteme, rtob, …) is not
yet surveyed; it will surface on the first full-family `dedupe-scan` run.

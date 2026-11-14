---
references:
    - specs/dry/requirements.md
    - specs/dry/design.md
    - specs/dry/decision_log.md
---
# dry Implementation Tasks

## Detector

- [ ] 1. Author scripts/dedupe_scan.py <!-- id:dry01a -->
  - Enumerate source files across repo paths / a parent dir; exclude node_modules, .git, build/dist, .svelte-kit, __pycache__, .venv, lockfiles, binary/asset extensions
  - Normalize (collapse whitespace; canonicalize import/require specifiers) + SHA-256 for exact clusters
  - Near-match via difflib.SequenceMatcher on canonical text, threshold default 0.85
  - Union-find into clusters; classify extraction vs consolidation (heuristic); emit JSON per design data model
  - Stream: 1
  - Requirements: [1.1](requirements.md#1.1), [1.2](requirements.md#1.2), [1.3](requirements.md#1.3), [1.4](requirements.md#1.4)
  - [ ] 1.1. Write fixture tests (red) <!-- id:dry01b -->
    - Fixture A: three mini-repos, md_renderer.py differing only by import path -> one extraction cluster, all three members, similarity >= 0.85
    - Fixture B: two mini SvelteKit-ish trees sharing block/heading logic -> one consolidation cluster
    - Exclusion: node_modules / lockfile content never clustered
    - Stream: 1
    - Requirements: [1.5](requirements.md#1.5), [1.6](requirements.md#1.6)
  - [ ] 1.2. Implement dedupe_scan.py to pass the fixtures (green) <!-- id:dry01c -->
    - Blocked-by: dry01b (Write fixture tests red)
    - Stream: 1

- [ ] 2. Author claude/skills/dedupe-scan/SKILL.md <!-- id:dry02a -->
  - Four phases per design: resolve scope, run detector, classify + confirm with user, present report; writes no files
  - Frontmatter name/description matching the tree; approval gate before any downstream skill is named as "the" fix
  - Blocked-by: dry01c (Implement dedupe_scan.py green)
  - Stream: 1
  - Requirements: [1.3](requirements.md#1.3), [1.4](requirements.md#1.4), [1.7](requirements.md#1.7)

## Resolution skills

- [ ] 3. Author claude/skills/shared-extract/SKILL.md <!-- id:dry03a -->
  - Five phases per design: take cluster, diff to shared core, propose home (approval), extract + refactor preserving deltas, verify via each repo's tests/build
  - Stream: 1
  - Requirements: [2.1](requirements.md#2.1), [2.2](requirements.md#2.2), [2.3](requirements.md#2.3), [2.4](requirements.md#2.4)

- [ ] 4. Author claude/skills/repo-consolidate/SKILL.md <!-- id:dry04a -->
  - Four phases per design: capability map, recommend canonical + classify absorb/already-present/retire, ordered migration plan (approval), optional execute
  - Stream: 1
  - Requirements: [3.1](requirements.md#3.1), [3.2](requirements.md#3.2), [3.3](requirements.md#3.3)

## Prevention and parent model

- [ ] 5. Author scripts/parents.json registry + schema check <!-- id:dry05a -->
  - Seed with siteme (declarative-site-engine) and brandme (branded-pdf-pipeline); each entry has repo, capabilities, spawn
  - Schema check: parses; every entry has the three keys
  - Stream: 2
  - Requirements: [4.3](requirements.md#4.3), [5.3](requirements.md#5.3)

- [ ] 6. Author claude/skills/simplicity-guard/SKILL.md <!-- id:dry06a -->
  - Three phases per design: take intent, match registry, recommend spawn-from-parent or genuinely-new; offer to register a missed parent
  - Blocked-by: dry05a (Author scripts/parents.json registry + schema check)
  - Stream: 2
  - Requirements: [4.1](requirements.md#4.1), [4.2](requirements.md#4.2)

- [ ] 7. brandme spawner: make new-docapp NAME=... <!-- id:dry07a -->
  - Consumer repo with the shared pipeline imported (post task 10) and only doctype templates + defaults.yaml to fill
  - Mirrors siteme's make new-site / SITE_DIR pattern
  - Blocked-by: dry10a (Run shared-extract on cluster B)
  - Stream: 2
  - Requirements: [5.1](requirements.md#5.1), [5.2](requirements.md#5.2)

## Conventions

- [ ] 8. README registry entries for the four DRY skills <!-- id:dry08a -->
  - List dedupe-scan, shared-extract, repo-consolidate, simplicity-guard in the agentic-coding README skill registry
  - Blocked-by: dry02a, dry03a, dry04a, dry06a
  - Stream: 1
  - Requirements: [6.3](requirements.md#6.3)

## First application (the proof cases)

- [ ] 9. Run dedupe-scan against cluster B repos <!-- id:dry09a -->
  - brandme, toes, invoicer; confirm it reports the PDF-pipeline cluster (md_renderer + pdf_renderer from all three), extraction-shaped
  - Blocked-by: dry02a (Author claude/skills/dedupe-scan/SKILL.md)
  - Stream: 1
  - Requirements: [1.5](requirements.md#1.5)

- [ ] 10. Run shared-extract on cluster B (brandme home) <!-- id:dry10a -->
  - Extract {config, models, branding, formatting, md_renderer, pdf_renderer, shadow}; refactor toes + invoicer to import; verify each repo's tests/build
  - Blocked-by: dry03a (Author claude/skills/shared-extract/SKILL.md), dry09a (Run dedupe-scan against cluster B repos)
  - Stream: 1
  - Requirements: [2.5](requirements.md#2.5)

- [ ] 11. Run repo-consolidate on cluster A (sdd-ui / spadre) <!-- id:dry11a -->
  - Emit the consolidation plan: spadre canonical; sdd-ui agent-bridge, Orbit panel, dispatch sheet classified absorb
  - Plan only in this baseline; execution is a follow-on spec
  - Blocked-by: dry04a (Author claude/skills/repo-consolidate/SKILL.md)
  - Stream: 1
  - Requirements: [3.4](requirements.md#3.4)

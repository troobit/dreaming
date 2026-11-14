# Design: dry

## Overview

Four new skills in `agentic-coding/claude/skills/` plus a small detection helper script and a
parent-capability registry. The skills are LLM-instruction markdown like every other skill in the
tree — no new runtime. The one executable is `scripts/dedupe_scan.py`, a dependency-free Python
script that does the file hashing/similarity so the `dedupe-scan` skill has deterministic
evidence instead of eyeballing diffs. The parent-repo model generalizes what `siteme` already
does (`make new-site`, `SITE_DIR`) into a convention any capability-owning repo can adopt.

The four skills form a pipeline: **scan → resolve → guard**. `dedupe-scan` finds clusters;
`shared-extract` and `repo-consolidate` resolve the two cluster shapes; `simplicity-guard` stops
new clusters forming by routing new work to a parent.

## Architecture

### Skills touch map

| Path | Action | Requirements |
|---|---|---|
| `claude/skills/dedupe-scan/SKILL.md` | new | 1 |
| `claude/skills/shared-extract/SKILL.md` | new | 2 |
| `claude/skills/repo-consolidate/SKILL.md` | new | 3 |
| `claude/skills/simplicity-guard/SKILL.md` | new | 4, 5.3 |
| `scripts/dedupe_scan.py` | new — hash + similarity, emits a JSON cluster report | 1.2, 1.3 |
| `scripts/parents.json` (or per-repo `.agentic.json` key) | new — parent→capability registry | 4.3, 5.3 |
| `README.md` (this repo) | registry entries for the four skills | 6.3 |
| `brandme/Makefile` + spawn script | new — `make new-docapp NAME=…` consumer spawner | 5.1, 5.2 |

### The parent-repo model

A **parent** owns a capability and exposes one spawn mechanism. A **consumer** is what the
spawner emits: it carries only its own content/config and references the parent for shared code.

`siteme` is the reference implementation of this model and needs no change to participate:
- Spawn mechanism: `make new-site NAME=mysite` (starter template) and `SITE_DIR=<path>` for an
  external consumer folder.
- Consumer holds: a `sites/<name>/` tree of YAML (content + theme), nothing else.
- Shared code: the SvelteKit engine in `siteme/src/lib/engine/`, referenced by path/workspace.

`brandme` adopts the same shape for the branded-PDF capability:
- Spawn mechanism (new): `make new-docapp NAME=…` → a consumer repo with the shared pipeline
  imported and only doctype templates + `defaults.yaml` to fill in.
- Shared code: the extracted pipeline (Req 2) — `config`, `models`, `branding`, `formatting`,
  `md_renderer`, `pdf_renderer`, `shadow` — living in brandme (the superset) and imported by
  consumers.

The registry (`scripts/parents.json`) maps each parent to the capabilities it owns and its spawn
mechanism, so `simplicity-guard` can answer "is there a parent for X?" without re-deriving it:

```json
{
  "parents": [
    { "repo": "platform/siteme", "capabilities": ["declarative-site-engine"],
      "spawn": "make new-site NAME=<name>", "ref": "SITE_DIR" },
    { "repo": "platform/brandme", "capabilities": ["branded-pdf-pipeline"],
      "spawn": "make new-docapp NAME=<name>", "ref": "workspace-import" }
  ]
}
```

### Detection algorithm (`dedupe-scan` → `scripts/dedupe_scan.py`)

1. **Enumerate.** For each repo path (or each subdir of a parent dir that is a git repo), walk
   source files. Exclude by path: `node_modules`, `.git`, `build/`, `dist/`, `.svelte-kit`,
   `__pycache__`, `.venv`, lockfiles (`pnpm-lock.yaml`, `uv.lock`, `package-lock.json`), and
   binary/asset extensions.
2. **Normalize + hash.** Normalize each file (strip comment-only lines is *not* done — too
   lossy; instead collapse whitespace and rewrite import/require specifiers to a canonical form)
   and SHA-256 it. Files sharing a hash are an **exact cluster**.
3. **Near-match.** For files that did not exact-cluster, compare normalized content pairwise with
   a similarity ratio (Python `difflib.SequenceMatcher` on the canonical text). Pairs above a
   threshold (default 0.85) join a **near cluster**. This is what catches `md_renderer.py`
   (brandme vs toes) — identical except the import line.
4. **Cluster + classify.** Union-find over exact and near edges. For each cluster spanning ≥2
   repos, record members (repo + path), the min similarity in the cluster, and a sample diff
   (the two most-similar members). Classify: if the cluster is a set of *library modules* shared
   across apps → **extraction-shaped** (→ `shared-extract`); if it is the *application surface*
   of two whole apps (routes + components doing the same job) → **consolidation-shaped**
   (→ `repo-consolidate`). Classification is a heuristic the skill confirms with the user.
5. **Report.** Emit JSON (for the skill to read) and a human summary. The skill presents it; it
   writes no files (Req 1.7).

## Components

### `dedupe-scan`

Phases:
1. **Resolve scope** — take repo paths or a parent dir; confirm the set with the user.
2. **Run detector** — invoke `scripts/dedupe_scan.py <paths…>`; read the JSON cluster report.
3. **Classify + confirm** — for each cross-repo cluster, state the shape (extraction vs
   consolidation) and the recommended skill; ask the user to confirm or override.
4. **Present report** — clusters sorted by (cross-repo, similarity desc), each with members,
   metric, sample diff, and next-skill pointer. No files written.

### `shared-extract`

Phases:
1. **Take a cluster** — from a `dedupe-scan` report or named files.
2. **Diff to the shared core** — align members, separate the common body from per-repo deltas.
3. **Propose a home** — existing parent (preferred) or new package; present for approval.
4. **Extract + refactor** — write the shared module; point each repo at it, preserving deltas.
5. **Verify** — run each affected repo's tests/build; report pass/fail per repo.

### `repo-consolidate`

Phases:
1. **Capability map** — for each repo, enumerate capabilities (routes, CLI verbs, modules);
   mark overlap vs unique.
2. **Recommend canonical** — the superset / intended generalisation; classify each capability as
   absorb / already-present / retire.
3. **Migration plan** — ordered ports, each with a verification step; present for approval.
4. **Execute (optional)** — port approved capabilities; leave a retirement note for the rest.

### `simplicity-guard`

Phases:
1. **Take intent** — a description of new work or a draft repo path.
2. **Match registry** — read `scripts/parents.json`; find a parent whose capability matches.
3. **Recommend** — spawn-from-parent (name the mechanism) or "genuinely new"; if a match was
   missed, offer to add the parent to the registry.

## Data model

**Cluster report (JSON, from `dedupe_scan.py`):**
```json
{ "clusters": [ {
    "id": "pdf-pipeline",
    "shape": "extraction",            // extraction | consolidation
    "similarity_min": 0.97,
    "members": [ { "repo": "platform/brandme", "path": "brandme/md_renderer.py" },
                 { "repo": "platform/toes",    "path": "lib/md_renderer.py" } ],
    "sample_diff": { "a": "…/brandme/md_renderer.py", "b": "…/toes/lib/md_renderer.py",
                     "unified": "@@ … from .formatting import format_aud\n+from lib.formatting …" }
} ] }
```

**Parent registry (`scripts/parents.json`):** see the example in Architecture. One entry per
parent; `capabilities` is a list of kebab-case capability names the guard matches against.

## Reference clusters (the proof cases)

These are confirmed in the working tree and double as the detector's acceptance fixtures.

**Cluster B — extraction (Req 1.5, 2.5).** Shared modules across `brandme`, `toes`, `invoicer`:

| module | brandme | toes | invoicer |
|---|---|---|---|
| `config.py` | 192 ln | 110 ln | 102 ln |
| `models.py` | 95 ln | 98 ln | 52 ln |
| `branding.py` | 176 ln | 165 ln | — |
| `formatting.py` | 14 ln | 14 ln | 10 ln |
| `md_renderer.py` | 30 ln | 30 ln | 21 ln |
| `pdf_renderer.py` | 396 ln | 221 ln | 61 ln |
| `shadow.py` | 222 ln | 166 ln | — |

`md_renderer.py` (brandme vs toes) differs only in the import line
(`from .formatting import format_aud` → `from lib.formatting import format_aud`). brandme is the
superset (adds CV, QR, icons, animate) → it is the extraction home.

**Cluster A — consolidation (Req 1.6, 3.4).** Both SvelteKit + Svelte 5; both do
markdown→line-anchored blocks, heading/outline extraction, mermaid-fence pull-out, HTML
sanitization, image-ref rewrite:

| concern | sdd-ui | spadre |
|---|---|---|
| markdown stack | `marked` + `isomorphic-dompurify` | `unified/remark/rehype` + shiki |
| blocks + lines | `src/lib/markdown.ts` (`segmentMarkdown`) | `packages/core/src/markdown/render.ts` |
| outline | `src/lib/outline.ts` (`headingsOf`, `applyFolds`) | `apps/web/src/lib/outline.ts` (`outlineTree`) + core `headings.ts` |
| unique to sdd-ui | agent bridge, Orbit panel, dispatch sheet, model picker | — |
| unique to spadre | — | threads, suggestions, WebAuthn, versions/diff, CLI, Azure/OpenTofu |

spadre is the superset and the intended generalisation → canonical. sdd-ui's unique surface is
the **absorb** set.

## Error handling

| Failure | Behavior |
|---|---|
| A path in the scan set is not a git repo / missing | Skip with a warning; continue with the rest (Req 1.1) |
| Similarity threshold yields a huge cluster (>N files) | Report it but flag for manual review; do not auto-classify (Req 1.4) |
| `dedupe_scan.py` missing or errors | Skill falls back to manual hash/diff via shell and says the helper is unavailable (Req 1.2) |
| Extraction home ambiguous (no clear superset) | `shared-extract` presents the candidates and asks; no write until chosen (Req 2.2) |
| A repo's tests/build fail after refactor | Mark that repo incomplete; leave its revert point noted (Req 2.4) |
| Registry has no matching parent | `simplicity-guard` says "genuinely new" and offers to register a new parent (Req 4.2) |

## Testing strategy

- **`dedupe_scan.py`** is the only executable and gets real tests:
  - Fixture A (cluster B): three mini-repos with `md_renderer.py` differing only by import path →
    assert one extraction cluster containing all three, similarity ≥ 0.85 (Req 1.5).
  - Fixture B (cluster A): two mini SvelteKit-ish trees sharing block/heading logic → assert a
    consolidation cluster (Req 1.6).
  - Exclusion test: `node_modules`/lockfile content is never clustered (Req 1.1).
- **Skills** are instruction markdown — exercised by running them against the real clusters A and
  B (the first application, `tasks.md`), not unit tests. The detector's fixtures are what keep the
  skill's evidence deterministic.
- **Registry** — a schema check that `parents.json` parses and each entry has `repo`,
  `capabilities`, `spawn` (Req 5.3).

## Requirements traceability

| Requirement | Design element |
|---|---|
| 1.1–1.7 | Detection algorithm (enumerate/normalize/hash/near-match/classify/report) + `dedupe_scan.py` |
| 2.1–2.5 | `shared-extract` phases; cluster B reference (brandme home) |
| 3.1–3.4 | `repo-consolidate` phases; cluster A reference (spadre canonical) |
| 4.1–4.3 | `simplicity-guard` phases + parent registry |
| 5.1–5.3 | Parent-repo model; brandme spawner; `parents.json` |
| 6.1–6.3 | Skills touch map (paths, frontmatter, approval gates) + README registry |



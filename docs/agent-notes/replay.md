# Behavioural replay gate

- `ReplayGate.check(change)` runs outside a store write transaction before adoption
  checks and returns a hold reason or `None`. Code changes and live file-finding
  evidence without observations skip replay. Conservative path matching treats
  Markdown, `.mdc`, `.rules`, `rules/` and `lenses.toml` as instruction changes.
- A replay uses immutable before/after blobs from the authored commit and its
  parent, never current checkout files or unrelated instructions. The source is
  opened through apsis or a registered shared-json document. Only a complete first
  user entry consisting of untruncated text is replayable; supplied context,
  unattributed content, missing openings and explicit truncation warnings exclude it.
- Source sessions are deduplicated by interaction/thread and sampled deterministically.
  Live evidence and forgetting are checked before every call and after judgements.
  A previously passed batch cannot bypass later forgetting. File/observation
  availability and imported-evidence markers fail closed.
- Each sample makes baseline, changed and reviewer calls through the existing
  Dispatcher with empty tool lists, private adapter directories and existing
  isolation probes. Model provider transport remains available; model tools,
  filesystem writes and agent network tools do not. Baseline and changed calls
  use one backend without transport fallback; the separate judge uses the
  configured reviewer backend choices and normal reviewer fallback.
- The reviewer uses its snapshotted prompt plus a fixed replay protocol/schema
  (`baseline`, `changed`, `reason`). It sees the problem quote and the two outputs,
  never the source transcript. Only `baseline=recurs` enters the denominator;
  changed `unclear` counts as recurrence. Identical outputs cannot resolve a
  recurring baseline even if a model claims otherwise.
- Lens changes rerun the reader on complete stored request envelopes with the
  affected lens replaced. Role-prompt changes replace only the exact stored prompt
  prefix and rerun that role with tools disabled. Old calls lacking complete
  envelopes and changes affecting multiple roles are held rather than guessed.
  Every original input session remains in replay call lineage; forgetting even
  a secondary input excludes that stored envelope before it can reach a model.
- `replay_batches` fixes the terminal result per change/authoring. Calls use stable
  Replay step keys and save outputs before judging; recovery reuses successes,
  while an indeterminate call holds without a repeat. Terminal batches finish their
  Replay steps so the Engine does not execute them independently. Budget preflight
  reserves inputs and response space only for pending calls; every attempt is
  charged through the shared run budget. `replays` stores raw counts, actual model
  attribution and token use; the report labels the result association.

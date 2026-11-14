# Observations, reading and knowledge

- `observe.py` checks source identity, canonical entry location, original entry
  identifier, lens version, quote and attribution against unredacted entries.
  The dispatcher checkpoint callback runs this gate before persisting a redacted
  response. Only that program-generated checkpoint is applied on recovery;
  verification never compares a redacted quote to a raw source.
- apsis tool results store text in `content` and can sit inside user-role entries.
  Block attribution takes precedence over that envelope. Check confirmation
  requires recorded success/exit metadata; assistant prose cannot confirm it.
- Observation topic and source attribution are separate. Canonical ids prevent
  duplicate observations and patterns across rereads. Original source entry ids
  support registrations; source digests label changed or unavailable references
  without deleting accepted evidence. Earlier dreams have separate pattern scope
  and cannot reinforce knowledge about the sessions they read.
- `schedule_reads` inserts initial Read steps and a dependent Review step.
  Each successful part adds its next part and extends Review's dependencies in
  the same result transaction. Only the final part advances source-copy cursors.
  Schema failures retain their lens versions and are not selected again by
  default. Pending parts resize against the smallest live fallback context.
- `KnowledgeRules` applies operations with savepoints inside the result
  transaction. Promotion counts distinct interactions, enforces source scope
  and confirmed success for `approach_worked`, and preserves corrections,
  dismissals, append-only revisions and optimistic versions. Destructive
  revisions over the per-scope cap remain queued. Learning dispositions have
  an explicit setter and a read-only eligibility view for dated deferrals.
- `ReviewHandler` supplies bounded summaries, representative quotes across
  interactions, feedback and permitted earlier run records. New clusters have
  a reserved context share; omitted counts are grouped by lens. Pattern merges
  retain aliases so rereads do not recreate merged identities. Decisions retain
  rejected/deferred reasons and candidate ranking signals. Research/Author
  follow-ups commit with revisions; their handlers belong to the API phase.
- `dream trial reader` uses committed instructions, one chosen backend and a
  read-only store snapshot. It reports schema/quote rates and total tokens;
  harness probe files live only in a temporary home. It never opens the user's
  store for writes or creates its home. Normal tests use fake models.

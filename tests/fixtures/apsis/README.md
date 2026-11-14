# Recorded apsis contract fixtures

Copied unchanged from `ArjenSchwarz/orbit` revision
`686ba0917a0eabefd271a3a2035ce52d36a2f266`:

- `internal/sharedjson/testdata/golden/*.json`: source-format golden outputs.
- `internal/sharedjson/testdata/handwritten/{listing-all-projects,exec-session,lmstudio-session-view}.json`:
  listing, correlated exec and logical LM Studio session outputs.

These are upstream synthetic fixtures, not the user's session history. The schema
is vendored in `src/dreaming/apsis_shared_v1.schema.json`; the upstream MIT licence
is in `src/dreaming/APSIS-LICENSE.txt`. No parsing code is copied. Refresh the
schema and these fixtures together when upgrading the contract. Unknown fields
and block types remain accepted within schema version 1; a new version requires
an explicit consumer update.

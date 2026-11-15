# Knowledge API and exchange

- `api.handle_request(store, config, request)` handles the six `knowledge.*`
  operations. `dream api` supplies one JSON request and emits one response.
  Requests carry `dreaming_api: 1` and `op`, with operation fields at the top
  level. Scope queries accept `domain`, `project`, and `workflow`; configured
  project mappings supply a missing domain. Every constrained dimension must
  match. Missing or unresolved item scope never becomes global.
- Recall/search use live exclusion views and deterministic specificity,
  confidence, chronological recency, then identifier ordering. The token limit
  covers canonical UTF-8 JSON for the returned item list (`utf8_bytes/4`, rounded
  up); an empty list counts zero. Selection stops when the next ordered item
  would exceed the limit. Evidence includes a bounded quote, modality and source
  availability, without loading transcripts. `get` exposes retained status even
  for withheld items while excluding forgotten evidence references.
- Feedback references are `{item_id, version, usage: cited|present_only}` under
  `revisions`, keyed by `feedback_id`. Outcomes queue reassessment and never
  mutate observations into successful-use evidence. A correction, scope change
  or dismissal targets one current revision and appends history with actor
  `feedback`; stale corrections fail. Optional evidence exclusion affects other
  supported items immediately. Learning dispositions use `learning_id` and
  `disposition` on the same operation.
- `exchange.py` exports a deterministic `dreaming-export` version 1 document.
  Import and export use the `export` field. Exchange includes retained evidence,
  revisions, feedback, dispositions, attribution registrations and availability
  markers. Runs carry terminal provenance only; workers, snapshots, raw calls
  and local document paths do not cross the boundary. Derived forgotten-session
  exclusions become explicit session markers in the export.
- Import is one transaction and preserves existing identifiers without merging
  divergent records. Imported knowledge and evidence receive internal provenance
  flags, omitted from the exchange document so repeated export/import is stable.
  Imported knowledge remains retrievable; imported observations and file findings
  cannot supply fresh promotion counts. Forward pattern references use deferred
  foreign-key checks at commit.

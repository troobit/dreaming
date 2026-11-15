# Dream reports

- `report.render_report(store, run_id, sections=...)` reads durable run state and
  produces standalone HTML. `write_report` writes it atomically under the private
  home's `reports/` directory with mode 0600. Both reject unsafe identifiers;
  report destinations are checked against normal, worktree and bare repositories.
- The Engine worker supplies `write_report` as its exit finalizer. Report
  generation makes no model calls. Browser opening is explicitly enabled by the
  caller and still restricted to requester `user`; missing browsers only log.
- The optional section map contains trusted internal HTML, keyed by `changes`,
  `effect` or `inventory` (other existing section keys also work). Extension
  renderers must escape their own stored values. Default sections cover retained
  revisions, learning, observations, call usage, candidates, earlier branches,
  forgotten sessions and currency notes, with empty states where appropriate.
- Session usage comes from reader call lineage. Run statistics may override the
  four session counts and supply `selection_skipped` records for skips that never
  made a call. No raw role response, source transcript or configured names list
  is copied into the report. All ordinary stored text is escaped and scrubbed;
  source links render only for HTTP(S) URLs.
- CSS and the page shell are vendored from agentic-coding revision
  `5d07d2f7f31675709f57b35579f72058e846fa67`, with only attribution, formatting and
  footer changes. `report/UPSTREAM.txt` records that upstream declared no licence
  at inspection; no licence grant is invented. Runtime does not need that repo.

# Currency review

- A currency dream is `dream.start` with `kind: "currency"` (optional `targets`
  names). `runs.kind='currency'`; `operations.work` skips selection and Read and
  `currency.schedule_currency` adds one Research step per `currency_targets`
  entry (config: name, repository key, kind, paths). Steps are Research, Author
  per candidate, then Adopt. Adopt already holds currency changes
  (`external_input_requires_manual_merge`), so they stay on `dream/<run>`.
- The program, not the model, fetches cited pages (in the researcher checkpoint,
  so the text is durable with the response). Pages go to `currency_pages`;
  `max_sources` bounds fetch attempts per run; later URLs are stored as
  `unavailable: source_limit`. `fetch_page` refuses non-public addresses.
- A finding is rejected if any web passage is absent from stored text (whitespace
  normalised only). Independence = distinct registrable domain (small built-in
  multi-part suffix list, no PSL) and non-overlapping passages. Fewer than
  `currency_min_sources` independent sources keeps a `note`. Non-web sources are
  ignored for support.
- Finding id = hash of (target, normalised claim), so equivalent findings reuse it.
  `dismissed` or `branch_deleted` findings, or a committed change whose branch is
  gone and unmerged, are deferred unless a new registrable domain appears.
  `discard` keeps proposed findings as `branch_deleted` instead of deleting them.
- Adopt step is created by the last Research step to finish: apply, fail, or be
  omitted for budget (`CurrencyResearchHandler.omitted`, called by
  `Engine._omit_for_budget`), so earlier targets' changes are still committed.

# Effect measurement (`effect.py`)

- Counts use observations by `observed_at` and `sessions_seen` (state read, not
  forgotten, `started_at`) inside the pattern's `lens_scope`, split at `applied_at`.
  Insufficient evidence reasons: `file_only`, `no_baseline_sessions`,
  `too_few_sessions`, `no_baseline_observations` (checked in that order).
- Re-candidacy uses the latest applied change: after sessions >=
  `recurrence_sessions`, after rate >= before rate, and total changes for the
  pattern < `max_changes_per_pattern`. `review_context` adds `effect` to existing
  pattern summaries; the worker passes `render_effect` as the report's `effect`
  section. The report shows selection diagnostics (source warnings) so unavailable
  optional integrations are visible (14.4).

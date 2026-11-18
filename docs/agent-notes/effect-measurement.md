# Effect measurement, re-candidacy and calibration

Task 27 of `specs/dreaming/tasks.md`: `effect.py`, its use in `review.py` and the report's `effect` and `outcomes` sections. Tests: `tests/test_effect.py`. Decisions Q131, Q132.

## Measuring a change (`effect.Measure`)

- Only changes with a `pattern_id` and `applied_at` are measured (currency changes have no pattern).
- Sessions in scope: `sessions_seen` rows with `state = 'read'`, `forgotten = 0` and the pattern's lens in `lens_versions` (only content copies have lens versions), one per interaction (`thread_id`, else `source:id`), dated by the earliest `started_at`.
- Observations: `knowledge.COUNTED_OBSERVATIONS` of the pattern, each dated by its session's `started_at` (falling back to `observed_on`), so both counts use the same split.
- Before = started before `applied_at`; after = from `applied_at` until `reverted_at` (if any). Times are parsed with `sessions.parse_time`, because stored timestamps mix `Z` and `+00:00`; string comparison in SQL would be wrong.
- `insufficient` (checked in this order): no cited observation (file findings only); no session before; fewer than `min_sessions_for_rate` before; fewer after (at least 1); no observation before.
- `recurring` = sufficient, at least `recurrence_sessions` (at least 1) after, and after rate ≥ before rate.

## Re-candidacy (11.2)

- `effect.recurs(conn, cfg, pattern)` measures the latest applied, unreverted change and returns it only when recurring.
- `review._Applier.check_change` refuses a changed pattern unless `recurs` says so; `max_changes_per_pattern` is checked after, so a recurring pattern still stops at the cap.
- `review._changes(conn, cfg)` puts `effect` (`Effect.to_dict()`, labelled "association, not proof") on each applied change the reviewer sees; `pattern_status` is `recurring` or `changed`.

## Calibration and outcomes

- `session_distribution` = `(distinct sessions, patterns)` pairs from `knowledge.pattern_signals`.
- `replay_against_recurrence` = for each applied change with `replays` rows: sessions replayed (`recurs_without = 1`), how many recurred with the change, and `recurs`/`reduced`/`insufficient: …` from the effect.
- `stage_outcomes(conn, run)` counts evidence gathering, reflection, selection and adoption for one run, with reasons for deferred/discarded candidates and waiting/withheld/failed changes. `review.run_records` includes it as `outcomes`; the report shows it under "Outcomes by stage".

## Gotchas

- The report measures every applied change in the store as of rendering time, not only this run's; a re-rendered old report shows newer numbers.
- `Measure` caches lens scopes per instance; create a new one per query that must see fresh rows.

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_reader import env as reader_fixture

from dreaming.effect import (
    effect_measurement,
    pattern_effects,
    recandidates,
    render_effect,
    replay_versus_recurrence,
    session_distribution,
)

reader_env = reader_fixture

APPLIED = "2026-11-10T00:00:00+00:00"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


class Seed:
    def __init__(self, store):
        self.store, self.n, self.o = store, 0, 0
        store.connection.execute("INSERT OR IGNORE INTO patterns(id,key) VALUES ('p','workflow')")

    def session(self, day, *, project="/repo", state="read"):
        self.n += 1
        sid = f"s{self.n}"
        self.store.connection.execute(
            "INSERT INTO sessions_seen(source,session_id,thread_id,project,state,started_at) "
            "VALUES ('claude',?,?,?,?,?)",
            (sid, "t" + sid, project, state, f"2026-11-{day:02d}T12:00:00+00:00"),
        )
        return sid

    def observe(self, sid, day, pattern="p"):
        self.o += 1
        self.store.connection.execute(
            "INSERT INTO observations(id,run_id,source,session_id,entry_id,source_location,"
            "representation_version,redacted_quote,kind,modality,outcome,lens_id,lens_version,"
            "pattern_id,backend,project,observed_at,observation_type) "
            "VALUES (?,'r','claude',?,'entry:0','entry:0',1,?,'user_statement','asserted',"
            "'unknown','preferences','1',?,'fake','/repo',?,'workflow')",
            (f"o{self.o}", sid, f"q{self.o}", pattern, f"2026-11-{day:02d}T12:00:00+00:00"),
        )

    def change(self, cid="c", *, applied=APPLIED, status="applied", evidence='["o1"]', **fields):
        self.store.connection.execute(
            "INSERT INTO changes(id,run_id,pattern_id,repo,status,applied_at,evidence,commit_sha,"
            "authoring_id) VALUES (?,'r','p','/repo',?,?,?,'abc',?)",
            (cid, status, applied, evidence, cid),
        )
        for key, value in fields.items():
            self.store.connection.execute(f"UPDATE changes SET {key}=? WHERE id=?", (value, cid))

    def window(self, before, after):
        """before/after: lists of observation counts per session (one session each)."""
        for counts, days in ((before, range(1, 10)), (after, range(11, 20))):
            for count, day in zip(counts, days, strict=False):
                sid = self.session(day)
                for _ in range(count):
                    self.observe(sid, day)


@pytest.fixture
def seeded(reader_env):
    store, config, roles, _ = reader_env
    config = replace(config, thresholds=replace(config.thresholds, min_sessions_for_rate=3))
    roles.config = config
    return store, config, Seed(store)


def measure(store, config, cid="c"):
    return effect_measurement(store, config, cid, now=NOW)


def test_raw_counts_before_and_after_are_labelled_association(seeded):
    store, config, s = seeded
    store.connection.execute(
        "UPDATE patterns SET lens_scope='{\"projects\":[\"/repo\"]}' WHERE id='p'"
    )
    s.window([1, 2, 0, 1], [0, 0, 1])
    s.session(12, project="/other")  # outside the pattern's scope
    s.session(15, state="skipped")  # never read
    s.change()
    result = measure(store, config)
    assert result["status"] == "measured" and result["label"] == "association"
    assert result["before"] == {"observations": 4, "sessions": 4}
    assert result["after"] == {"observations": 1, "sessions": 3}
    assert result["before_rate"] == 1.0 and result["after_rate"] == pytest.approx(1 / 3)


def test_scope_comes_from_the_pattern_lens_scope(seeded):
    store, config, s = seeded
    store.connection.execute(
        "UPDATE patterns SET lens_scope='{\"projects\":[\"/repo\"]}' WHERE id='p'"
    )
    s.window([1, 1, 1], [1, 1, 1])
    s.session(12, project="/other")
    s.change()
    assert measure(store, config)["after"]["sessions"] == 3


def test_forgotten_sessions_do_not_count(seeded):
    store, config, s = seeded
    s.window([1, 1, 1], [1, 1, 1, 1])
    store.connection.execute("UPDATE sessions_seen SET forgotten=1 WHERE session_id='s6'")
    s.change()
    assert measure(store, config)["after"]["sessions"] == 3


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        ([], [1, 1, 1], "no_baseline_sessions"),
        ([1, 1], [1, 1, 1], "too_few_sessions"),
        ([1, 1, 1], [1, 1], "too_few_sessions"),
        ([0, 0, 0], [1, 1, 1], "no_baseline_observations"),
    ],
)
def test_insufficient_evidence(seeded, before, after, reason):
    store, config, s = seeded
    s.window(before, after)
    s.change()
    result = measure(store, config)
    assert result["status"] == "insufficient_evidence" and result["reason"] == reason
    assert "before_rate" not in result


def test_file_only_findings_are_insufficient(seeded):
    store, config, s = seeded
    s.window([1, 1, 1], [1, 1, 1])
    store.connection.execute(
        "INSERT INTO file_findings(id,run_id,repo,revision,path,location,lens_id,finding) "
        "VALUES ('f1','r','/repo','rev','a.md','file','l','x')"
    )
    s.change(evidence='["f1"]')
    assert measure(store, config)["reason"] == "file_only"


def test_unapplied_or_unknown_changes_are_not_measured(seeded):
    store, config, s = seeded
    s.change("c", applied=None, status="proposed")
    with pytest.raises(KeyError):
        measure(store, config, "missing")
    assert measure(store, config)["status"] == "not_applied"


def test_recandidacy_at_or_above_pre_change_rate_up_to_max_changes(seeded):
    store, config, s = seeded
    config = replace(
        config,
        thresholds=replace(config.thresholds, recurrence_sessions=3, max_changes_per_pattern=2),
    )
    s.window([1, 1, 1], [1, 1, 1])  # rate 1.0 before and after
    s.change()
    found = recandidates(store, config, now=NOW)
    assert [(r["pattern_id"], r["earlier_change"]) for r in found] == [("p", "c")]
    assert found[0]["before_rate"] == found[0]["after_rate"] == 1.0
    s.change("c2", applied="2026-11-11T00:00:00+00:00", status="failed")
    assert recandidates(store, config, now=NOW) == []  # two changes: at the maximum


def test_no_recandidacy_when_rate_fell_or_too_few_later_sessions(seeded):
    store, config, s = seeded
    config = replace(config, thresholds=replace(config.thresholds, recurrence_sessions=4))
    s.window([1, 1, 1], [1, 1, 1])
    s.change()
    assert recandidates(store, config, now=NOW) == []  # only 3 later sessions
    config = replace(config, thresholds=replace(config.thresholds, recurrence_sessions=3))
    store.connection.execute("DELETE FROM observations WHERE observed_at>'2026-11-11'")
    assert recandidates(store, config, now=NOW) == []  # rate fell to zero


def test_pattern_effects_feed_the_reviewer_context(reader_env):
    from dreaming.review import review_context

    store, config, roles, _ = reader_env
    config = replace(
        config,
        thresholds=replace(config.thresholds, min_sessions_for_rate=3, recurrence_sessions=3),
    )
    roles.config = config
    s = Seed(store)
    s.window([1, 1, 1], [1, 1, 1])
    s.change()
    effects = pattern_effects(store, config, now=NOW)
    assert effects["p"]["measurement"]["status"] == "measured"
    assert effects["p"]["recandidate"] is True
    context = review_context(store, "r", config, roles, now=NOW)
    summary = next(c for c in context["patterns"] if c["pattern_id"] == "p")
    assert summary["effect"]["recandidate"] is True
    assert summary["effect"]["measurement"]["label"] == "association"


def test_calibration_distribution_of_distinct_sessions_per_pattern(seeded):
    store, config, s = seeded
    store.connection.execute("INSERT INTO patterns(id,key) VALUES ('q','other')")
    s.window([2, 1], [1])  # pattern p: 3 sessions, 4 observations
    sid = s.session(5)
    s.observe(sid, 5, "q")
    result = session_distribution(store)
    assert result["per_pattern"] == {"p": 3, "q": 1}
    assert result["histogram"] == {1: 1, 3: 1}


def test_calibration_replay_outcome_versus_later_recurrence(seeded):
    store, config, s = seeded
    s.window([1, 1, 1], [1, 1, 1])
    s.change()
    s.window([], [])
    store.connection.execute(
        "INSERT INTO replay_batches(change_id,authoring_id,status) VALUES ('c','c','passed')"
    )
    for i, (judgement, baseline) in enumerate(
        [("resolved", 1), ("recurs", 1), ("unclear", 1), ("resolved", 0)]
    ):
        sid = s.session(1 + i)
        store.connection.execute(
            "INSERT INTO replays(change_id,authoring_id,source,session_id,backend,judgement,"
            "baseline_recurs) VALUES ('c','c','claude',?,'fake',?,?)",
            (sid, judgement, baseline),
        )
    [row] = replay_versus_recurrence(store, config, now=NOW)
    assert row["change_id"] == "c" and row["replay_status"] == "passed"
    assert row["replayed_sessions"] == 3 and row["replay_recurs_share"] == pytest.approx(2 / 3)
    assert row["later_recurred"] is True


def test_effect_report_section_escapes_and_labels_association(seeded):
    store, config, s = seeded
    s.window([1, 1, 1], [1, 1, 1])
    s.change()
    store.connection.execute("UPDATE patterns SET key='<b>x</b>' WHERE id='p'")
    page = render_effect(store, config, now=NOW)
    assert "association" in page and "<b>x</b>" not in page and "&lt;b&gt;" in page
    assert "Calibration" in page
    from dreaming.report import render_report

    assert "association" in render_report(store, "r", sections={"effect": page})

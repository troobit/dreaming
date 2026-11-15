"""Effect measurement, re-candidacy and calibration statistics (6.7, 11.1,
11.2, 11.6; design, Risks and Assumptions)."""

import json
from pathlib import Path

import pytest
from dreamkit import EMPTY_REVIEW, ScriptedModel, build_config, instructions
from reportkit import page, section
from runkit import FakeClock, LocalPhase, add_run, make_runner
from sessiondocs import add_observation

from dreaming import effect, review, steps
from dreaming.steps import NewStep

RUN = "dream-now"
LENS = "preferences"
APPLIED = "2026-11-08T12:00:00+00:00"
BEFORE = [1, 2, 3, 4, 5]
AFTER = [9, 10, 11, 12, 13, 14]


@pytest.fixture
def cfg():
    return build_config(
        repositories={"agentic": {"path": "/work/agentic"}},
        thresholds={
            "min_sessions": 3,
            "min_sessions_for_rate": 4,
            "recurrence_sessions": 6,
            "max_changes_per_pattern": 2,
        },
    )


def pattern(db, pattern_id="p-uv"):
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at)"
        " VALUES (?, ?, ?, '2026-10-01')",
        (pattern_id, pattern_id, LENS),
    )


def session(db, sid, day, *, lens=LENS, forgotten=0):
    """A session read under `lens`, started on `day` of November 2026."""
    db.execute(
        "INSERT INTO sessions_seen (source, session_id, started_at, lens_versions,"
        " state, forgotten) VALUES ('claude', ?, ?, ?, 'read', ?)",
        (sid, f"2026-11-{day:02d}T10:00:00Z", json.dumps({lens: 1}), forgotten),
    )
    return sid


def observe(db, eid, sid, pattern_id="p-uv", key=None):
    add_observation(
        db,
        eid,
        "claude",
        sid,
        pattern_id=pattern_id,
        lens=LENS,
        kind="tool_result",
        project="/work/repo",
        observed_on="2026-11-10",
    )
    if key:
        db.execute(
            "UPDATE observations SET pattern_key = ? WHERE evidence_id = ?", (key, eid)
        )
    return eid


def sessions(db, prefix, days, *, observed=0):
    """Sessions started on `days`; the first `observed` of them show the
    pattern."""
    for index, day in enumerate(days):
        sid = session(db, f"{prefix}{index}", day)
        if index < observed:
            observe(db, f"o-{sid}", sid)


def applied(db, change_id="ch-1", *, at=APPLIED, evidence=("o-b0",), **fields):
    fields.setdefault("status", "applied")
    db.execute(
        "INSERT INTO changes (id, pattern_id, repo, evidence, status, applied_at,"
        " reverted_at, summary, created_at)"
        " VALUES (?, ?, 'agentic', ?, ?, ?, ?, ?, ?)",
        (
            change_id,
            fields.get("pattern_id", "p-uv"),
            json.dumps(list(evidence)),
            fields["status"],
            at,
            fields.get("reverted_at"),
            f"Change {change_id}",
            at,
        ),
    )
    return change_id


def file_finding(db, eid="ff1"):
    db.execute(
        "INSERT INTO file_findings (evidence_id, repo, revision, path, location,"
        " finding, lens, lens_version, pattern_id) VALUES (?, 'agentic', 'abc',"
        " 'skills/x.md', 'L3', 'links a missing file', 'broken-references', 1,"
        " 'p-uv')",
        (eid,),
    )
    return eid


# Before and after (11.1)


def test_counts_before_and_after_an_applied_change_are_raw_numbers(db, cfg):
    pattern(db)
    pattern(db, "p-other")
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", AFTER[:4], observed=1)
    session(db, "other-lens", 10, lens="tools")
    observe(db, "o-forgotten", session(db, "forgotten", 10, forgotten=1))
    observe(db, "o-other", "a3", "p-other")
    applied(db)
    (found,) = effect.measured(db, cfg)
    assert found.change_id == "ch-1" and found.pattern_id == "p-uv"
    assert (found.before_observations, found.before_sessions) == (4, 5)
    assert (found.after_observations, found.after_sessions) == (1, 4)
    assert (found.before_rate, found.after_rate) == (0.8, 0.25)
    assert found.insufficient is None
    assert found.recurring is False


def test_the_after_period_ends_when_the_change_was_reverted(db, cfg):
    pattern(db)
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", AFTER, observed=6)
    applied(db, status="rejected", reverted_at="2026-11-12T00:00:00+00:00")
    (found,) = effect.measured(db, cfg)
    assert (found.after_observations, found.after_sessions) == (3, 3)


@pytest.mark.parametrize(
    ("before", "observed", "after", "evidence", "reason"),
    [
        ([], 0, AFTER, ("o-a0",), "no session in its lens scope was read before"),
        ([1, 2], 2, AFTER, ("o-b0",), "2 sessions before the change, fewer than"),
        (BEFORE, 4, [9, 10], ("o-b0",), "2 sessions after the change, fewer than"),
        (BEFORE, 0, AFTER, ("o-a0",), "not observed before the change"),
        (BEFORE, 4, AFTER, ("ff1",), "cites file findings only"),
    ],
)
def test_too_little_evidence_is_reported_as_insufficient(
    db, cfg, before, observed, after, evidence, reason
):
    pattern(db)
    file_finding(db)
    sessions(db, "b", before, observed=observed)
    sessions(db, "a", after, observed=1)
    applied(db, evidence=evidence)
    (found,) = effect.measured(db, cfg)
    assert reason in found.insufficient
    assert found.recurring is False


def test_the_report_shows_effect_as_association(db, cfg):
    add_run(db, "dream-test", cfg, started_at="2026-11-15T09:00:00+00:00")
    pattern(db)
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", AFTER[:4], observed=1)
    applied(db)
    applied(db, "ch-2", at="2026-11-14T00:00:00+00:00")
    shown = section(page(db, "dream-test", cfg), "effect")
    assert "association, not proof" in shown
    assert "Change ch-1" in shown
    assert "4 observations in 5 sessions" in shown
    assert "1 observation in 4 sessions" in shown
    assert "0 sessions after the change, fewer than the 4 needed" in shown


# Re-candidacy (11.2)


def run_review(db, cfg, model, *, run_id=RUN):
    add_run(db, run_id, cfg, started_at="2026-11-15T09:00:00+00:00")
    runner = make_runner(
        db,
        Path("/unused"),
        run_id,
        cfg,
        phases={
            "review": review.ReviewPhase(instructions(cfg)),
            "research": LocalPhase(),
            "author": LocalPhase(),
            "adopt": LocalPhase(),
        },
        model=model,
        clock=FakeClock(),
        planner=lambda ctx: [NewStep("review", "review")],
    )
    return runner.execute()


def proposing(evidence):
    """A reviewer proposing a change for p-uv on `evidence`."""

    def call(role, data):
        candidate = {
            "pattern": "p-uv",
            "proposal": "change",
            "repo": "agentic",
            "summary": "a better uv rule",
            "evidence": list(evidence),
            "reason": "recurs after the change",
        }
        return {**EMPTY_REVIEW, "candidates": [candidate]}

    return ScriptedModel(call)


def recurring_pattern(db, after_observed):
    pattern(db)
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", AFTER, observed=after_observed)
    applied(db)
    new_cluster(db)


def new_cluster(db):
    """Something new, so the reviewer is called, in a session outside the
    pattern's lens scope."""
    observe(
        db, "o-new", session(db, "n0", 14, lens="tools"), pattern_id=None, key="new"
    )


def candidate_rejections(db):
    rows = db.execute("SELECT reason FROM rejections WHERE what = 'candidate'")
    return [row[0] for row in rows]


def test_a_pattern_recurring_at_its_pre_change_rate_is_a_candidate_again(db, cfg):
    recurring_pattern(db, after_observed=5)
    model = proposing(["o-a0", "o-a1", "o-a2"])
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    (summary,) = [p for p in data["patterns"] if p["pattern_id"] == "p-uv"]
    assert summary["status"] == "recurring"
    (earlier,) = summary["changes"]
    assert earlier["effect"] == {
        "before": {"observations": 4, "sessions": 5},
        "after": {"observations": 5, "sessions": 6},
        "insufficient": None,
        "recurring": True,
        "label": "association, not proof",
    }
    assert candidate_rejections(db) == []
    assert db.execute("SELECT pattern_id FROM candidates").fetchone()[0] == "p-uv"


@pytest.mark.parametrize(
    ("after_observed", "days"),
    [
        (2, AFTER),  # below the pre-change rate
        (5, AFTER[:5]),  # too few later sessions
    ],
)
def test_a_changed_pattern_below_its_rate_or_too_soon_is_not_a_candidate(
    db, cfg, after_observed, days
):
    pattern(db)
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", days, observed=after_observed)
    applied(db)
    new_cluster(db)
    model = proposing(["o-b0", "o-b1", "o-b2"])
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    (summary,) = [p for p in data["patterns"] if p["pattern_id"] == "p-uv"]
    assert summary["status"] == "changed"
    (reason,) = candidate_rejections(db)
    assert "(11.2)" in reason


def test_re_candidacy_stops_at_the_most_changes_per_pattern(db, cfg):
    recurring_pattern(db, after_observed=5)
    applied(db, "ch-0", at="2026-11-06T00:00:00+00:00")
    run_review(db, cfg, proposing(["o-a0", "o-a1", "o-a2"]))
    (reason,) = candidate_rejections(db)
    assert reason == "the pattern has had 2 changes, the most allowed"


# Calibration (design, Risks and Assumptions)


def test_calibration_gives_sessions_per_pattern_and_replay_against_recurrence(db, cfg):
    pattern(db)
    pattern(db, "p-two")
    sessions(db, "b", BEFORE, observed=4)
    sessions(db, "a", AFTER[:4], observed=1)
    observe(db, "o-t0", "b0", "p-two")
    observe(db, "o-t1", "b1", "p-two")
    applied(db)
    add_run(db, "dream-1", cfg, started_at="2026-11-08T09:00:00+00:00")
    for judgement in ("resolved", "resolved", "recurs"):
        db.execute(
            "INSERT INTO replays (run, change_id, recurs_without, judgement,"
            " created_at) VALUES ('dream-1', 'ch-1', 1, ?, ?)",
            (judgement, APPLIED),
        )
    db.execute(
        "INSERT INTO replays (run, change_id, recurs_without, judgement, created_at)"
        " VALUES ('dream-1', 'ch-1', 0, 'unclear', ?)",
        (APPLIED,),
    )
    assert effect.session_distribution(db) == [(2, 1), (5, 1)]
    assert effect.replay_against_recurrence(db, cfg) == [
        {
            "change_id": "ch-1",
            "summary": "Change ch-1",
            "replayed": 3,
            "recurred_with_change": 1,
            "later": "reduced",
        }
    ]
    shown = section(page(db, "dream-1", cfg), "effect")
    assert "Distinct sessions per pattern" in shown
    assert "Replay against later recurrence" in shown


# Outcomes by stage (11.6, 7.5)


def test_outcomes_by_stage_carry_the_reasons_for_deferrals(db, cfg):
    run_id = add_run(
        db, "dream-1", cfg, status="complete", started_at="2026-11-08T09:00:00+00:00"
    )
    for key, state, reason in [
        ("read:a", "done", None),
        ("read:b", "done", None),
        ("read:c", "skipped", "the Read share of the budget is used"),
    ]:
        steps.add(db, run_id, [NewStep(key, "read")])
        steps.finish(db, run_id, key, state, reason)
    pattern(db)
    sessions(db, "b", BEFORE[:2], observed=2)
    db.execute("UPDATE observations SET run = ?", (run_id,))
    steps.record_rejection(db, run_id, "read:a", "observation", None, "no match", {})
    steps.record_rejection(db, run_id, "review", "knowledge", "k1", "stale", {})
    for cid, status, reason in [
        ("c1", "authored", None),
        ("c2", "deferred", "the rule says uv already"),
    ]:
        db.execute(
            "INSERT INTO candidates (id, run, pattern_id, proposal, repo, summary,"
            " status, reason, created_at) VALUES (?, ?, 'p-uv', 'change', 'agentic',"
            " ?, ?, ?, ?)",
            (cid, run_id, f"candidate {cid}", status, reason, APPLIED),
        )
    applied(db, "ch-1")
    applied(db, "ch-2", status="waiting")
    db.execute(
        "UPDATE changes SET run = ?, reason = CASE id WHEN 'ch-2'"
        " THEN 'auto-apply is off' END",
        (run_id,),
    )
    outcomes = effect.stage_outcomes(db, run_id)
    assert outcomes == {
        "evidence_gathering": {
            "sessions_read": 2,
            "sessions_skipped": 1,
            "sessions_failed": 0,
            "observations_accepted": 2,
            "observations_rejected": 1,
        },
        "reflection": {
            "patterns_created": 0,
            "knowledge_revisions": 0,
            "operations_rejected": 1,
        },
        "selection": {
            "candidates": 2,
            "authored": 1,
            "deferred": 1,
            "discarded": 0,
            "rejected": 0,
        },
        "adoption": {"applied": 1, "waiting": 1, "withheld": 0, "failed": 0},
        "deferred": [
            {
                "stage": "selection",
                "subject": "candidate c2",
                "reason": "the rule says uv already",
            },
            {
                "stage": "adoption",
                "subject": "Change ch-2",
                "reason": "auto-apply is off",
            },
        ],
    }
    (record,) = review.run_records(db, RUN)
    assert record["outcomes"] == outcomes
    shown = section(page(db, run_id, cfg), "outcomes")
    assert "the rule says uv already" in shown and "auto-apply is off" in shown

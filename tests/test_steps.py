"""The step table, call records and crash consistency (1.6, 4.1, 5.4, 6.3, 7.5)."""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from runkit import (
    CallPhase,
    Crash,
    FakeClock,
    FakeModel,
    LocalPhase,
    add_run,
    fake_config,
    make_runner,
    reads,
    results,
    run_row,
    step_states,
)

from dreaming import run, steps, store
from dreaming.steps import NewStep


@pytest.fixture
def run_id(db):
    return add_run(db)


# The step table


def test_the_next_step_is_the_first_pending_one_whose_dependencies_are_finished(
    db, run_id
):
    steps.add(
        db,
        run_id,
        [
            NewStep("read:1", "read"),
            NewStep("read:2", "read"),
            NewStep("review", "review", depends_on=("read:1", "read:2")),
        ],
    )
    assert steps.next_ready(db, run_id).key == "read:1"
    steps.finish(db, run_id, "read:1", "done")
    assert steps.next_ready(db, run_id).key == "read:2"

    # Review waits for every Read step; a failed one is finished too (4.7).
    steps.begin(db, run_id, "read:2")
    assert steps.next_ready(db, run_id) is None
    steps.finish(db, run_id, "read:2", "failed", "schema failed twice")
    review = steps.next_ready(db, run_id)
    assert review.key == "review"
    assert review.depends_on == ("read:1", "read:2")


def test_steps_keep_the_order_they_were_added_in(db, run_id):
    keys = ["read:10", "read:9", "read:2"]
    steps.add(db, run_id, [NewStep(key, "read") for key in keys])
    assert [s.key for s in steps.load(db, run_id)] == keys


def test_a_step_depending_on_an_unknown_key_is_refused(db, run_id):
    with pytest.raises(ValueError, match="read:9"):
        steps.add(db, run_id, [NewStep("review", "review", depends_on=("read:9",))])
    assert steps.load(db, run_id) == []


def test_adding_a_key_again_changes_nothing(db, run_id):
    steps.add(db, run_id, [NewStep("read:1", "read", input_ref="first")])
    steps.finish(db, run_id, "read:1", "done")
    steps.add(db, run_id, [NewStep("read:1", "read", input_ref="second")])
    (step,) = steps.load(db, run_id)
    assert (step.state, step.input_ref) == ("done", "first")


def test_starting_a_step_counts_its_attempts(db, run_id):
    steps.add(db, run_id, [NewStep("read:1", "read")])
    steps.begin(db, run_id, "read:1")
    steps.recover(db, run_id, "2026-11-15T09:00:00+00:00")
    steps.begin(db, run_id, "read:1")
    (step,) = steps.load(db, run_id)
    assert (step.state, step.attempts) == ("running", 2)


# Results and follow-up steps commit together


def _follow_research(step, answers):
    return [NewStep("research:1", "research"), NewStep("research:2", "research")]


def test_follow_up_steps_commit_in_the_same_transaction_as_the_result(
    db, home, run_id, monkeypatch
):
    clock = FakeClock()
    model = FakeModel(clock)
    phases = {
        "read": CallPhase("reader"),
        "review": CallPhase("reviewer", follow=_follow_research),
    }
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases=phases,
        model=model,
        clock=clock,
        planner=reads(1),
    )
    finish = steps.finish

    def crash_on_review(conn, run, key, state, reason=None):
        if key == "review":
            raise Crash
        finish(conn, run, key, state, reason)

    monkeypatch.setattr(steps, "finish", crash_on_review)
    with pytest.raises(Crash):
        runner.execute()
    # The review's result, its follow-up steps and its done mark are all absent.
    assert "review" not in results(db)
    assert step_states(db) == {"read:1": "done", "review": "running"}

    monkeypatch.setattr(steps, "finish", finish)
    assert (
        make_runner(
            db,
            home,
            run_id,
            fake_config(),
            phases={**phases, "research": CallPhase("researcher")},
            model=model,
            clock=clock,
        ).execute()
        == "complete"
    )
    assert set(step_states(db)) == {"read:1", "review", "research:1", "research:2"}


# Call records


def test_the_calls_row_is_written_before_the_call(db, home, run_id):
    seen = []

    def before(role, prompt):
        row = db.execute(
            "SELECT step_key, attempt, role, backend, raw_output, ended_at FROM calls"
        ).fetchone()
        seen.append(dict(row))

    clock = FakeClock()
    model = FakeModel(clock, before=before)
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": CallPhase("reader")},
        model=model,
        clock=clock,
        planner=reads(1, review=False),
    )
    assert runner.execute() == "complete"
    assert seen == [
        {
            "step_key": "read:1",
            "attempt": 1,
            "role": "reader",
            "backend": "fake",
            "raw_output": None,
            "ended_at": None,
        }
    ]
    row = db.execute("SELECT * FROM calls").fetchone()
    assert json.loads(row["raw_output"]) == {"value": 0}
    assert row["outcome"] == "ok"
    assert (row["tokens_in"], row["tokens_out"]) == (100, 20)
    assert (row["session_ref"], row["model"]) == ("fake-session-0", "fake-model")


def test_stored_raw_output_is_applied_on_resume_without_a_model_call(db, home, run_id):
    crashes = iter([True])

    def crash_once():
        if next(crashes, False):
            raise Crash

    clock = FakeClock()
    model = FakeModel(clock)

    class CrashInApply(CallPhase):
        def apply(self, conn, step, answers):
            crash_once()
            return super().apply(conn, step, answers)

    phases = {"read": CrashInApply("reader")}
    with pytest.raises(Crash):
        make_runner(
            db,
            home,
            run_id,
            fake_config(),
            phases=phases,
            model=model,
            clock=clock,
            planner=reads(1, review=False),
        ).execute()
    assert len(model.prompts) == 1

    assert (
        make_runner(
            db, home, run_id, fake_config(), phases=phases, model=model, clock=clock
        ).execute()
        == "complete"
    )
    assert len(model.prompts) == 1
    assert results(db) == {"read:1": "[0]"}


def test_a_call_cut_off_before_its_answer_is_made_again_on_resume(db, home, run_id):
    crashes = iter([True])

    def before(role, prompt):
        if next(crashes, False):
            raise Crash

    clock = FakeClock()
    model = FakeModel(clock, before=before)
    phases = {"read": CallPhase("reader")}
    with pytest.raises(Crash):
        make_runner(
            db,
            home,
            run_id,
            fake_config(),
            phases=phases,
            model=model,
            clock=clock,
            planner=reads(1, review=False),
        ).execute()

    assert (
        make_runner(
            db, home, run_id, fake_config(), phases=phases, model=model, clock=clock
        ).execute()
        == "complete"
    )
    rows = db.execute("SELECT attempt, outcome FROM calls ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "interrupted"), (2, "ok")]
    assert results(db) == {"read:1": "[0]"}


def test_a_failed_call_records_its_error(db, home, run_id):
    def before(role, prompt):
        raise RuntimeError("connection reset")

    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": CallPhase("reader")},
        model=FakeModel(clock, before=before),
        clock=clock,
        planner=reads(1, review=False),
    )
    assert runner.execute() == "interrupted"
    row = db.execute("SELECT outcome, error, ended_at FROM calls").fetchone()
    assert row["outcome"] == "error"
    assert "connection reset" in row["error"]
    assert row["ended_at"] is not None
    assert "connection reset" in run_row(db)["error"]


# Crash consistency (design, Testing Strategy)


def _review_follow(step, answers):
    # The number of research questions depends on the answer, so a second,
    # different answer to the review would change the plan.
    research = [NewStep(f"research:{i}", "research") for i in range(answers[0] % 3 + 1)]
    keys = tuple(s.key for s in research)
    return [*research, NewStep("author:1", "author", depends_on=keys)]


def _author_follow(step, answers):
    return [NewStep("adopt:1", "adopt", depends_on=(step.key,))]


class Points:
    """Counts the moments a process could die and raises Crash at chosen ones."""

    def __init__(self, crash_at=()):
        self.crash_at = set(crash_at)
        self.count = 0

    def __call__(self, *args):
        self.count += 1
        if self.count in self.crash_at:
            raise Crash


def _dream(db, home, run_id, points, model, clock):
    phases = {
        "read": CallPhase("reader", hook=points),
        "review": CallPhase("reviewer", follow=_review_follow, hook=points),
        "research": CallPhase("researcher", hook=points),
        "author": CallPhase(
            "author", parts=("draft", "check"), follow=_author_follow, hook=points
        ),
        "adopt": LocalPhase(hook=points),
    }
    return make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases=phases,
        model=model,
        clock=clock,
        planner=reads(2),
    )


def _outcome(db, run_id):
    rows = db.execute(
        "SELECT key, phase, depends_on, state FROM steps WHERE run = ? ORDER BY key",
        (run_id,),
    )
    return [tuple(r) for r in rows], results(db), run_row(db, run_id)["status"]


@settings(max_examples=40, deadline=None)
@given(crash_at=st.lists(st.integers(1, 40), max_size=3, unique=True))
def test_resume_after_any_crash_matches_an_uninterrupted_run(
    tmp_path_factory, crash_at
):
    root = tmp_path_factory.mktemp("crash")
    home = root / "home"
    home.mkdir()

    def fresh(name):
        conn = store.open_store(root / f"{name}.db")
        return conn, add_run(conn)

    conn, run_id = fresh("whole")
    clock = FakeClock()
    model = FakeModel(clock, before=None)
    assert _dream(conn, home, run_id, Points(), model, clock).execute() == "complete"
    expected = _outcome(conn, run_id)
    conn.close()

    conn, run_id = fresh("crashed")
    points = Points(crash_at)
    model = FakeModel(clock, before=points)
    for _ in range(len(crash_at) + 1):
        # Each crash ends the process; the next invocation opens the store again.
        try:
            status = _dream(conn, home, run_id, points, model, clock).execute()
        except Crash:
            conn.close()
            conn = store.open_store(root / "crashed.db")
            continue
        break
    assert status == "complete"
    assert _outcome(conn, run_id) == expected
    conn.close()


def test_planning_happens_once(db, home, run_id):
    clock = FakeClock()
    model = FakeModel(clock)
    planned = []

    def planner(ctx):
        planned.append(ctx.run_id)
        return [NewStep("read:1", "read")]

    phases = {"read": CallPhase("reader")}
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases=phases,
        model=model,
        clock=clock,
        planner=planner,
    )
    assert runner.execute() == "complete"
    assert (
        make_runner(
            db,
            home,
            run_id,
            fake_config(),
            phases=phases,
            model=model,
            clock=clock,
            planner=planner,
        ).execute()
        == "complete"
    )
    assert planned == [run_id]


def test_a_step_without_a_handler_stops_the_run(db, home, run_id):
    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={},
        model=FakeModel(clock),
        clock=clock,
        planner=reads(1, review=False),
    )
    assert runner.execute() == "interrupted"
    assert "read" in run_row(db)["error"]
    assert run.exit_code("interrupted") == 1

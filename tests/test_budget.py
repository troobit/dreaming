"""Budgets, wall-clock time and usage-limit waits (1.2, 5.3, 5.6, Decision 16)."""

from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from runkit import (
    SCHEMA,
    CallPhase,
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

from dreaming import budget, run, steps, store
from dreaming.steps import NewStep


@pytest.fixture
def run_id(db):
    return add_run(db)


class Retrying:
    """Makes a second attempt of every call, as the dispatcher's retry does."""

    def prepare(self, ctx, step):
        backend = ctx.backends("reader")[0]
        key = steps.call_key(step.key)
        first = ctx.attempt(key, "reader", backend, "first", SCHEMA)
        second = ctx.attempt(key, "reader", backend, "again", SCHEMA)
        return [first.output["value"], second.output["value"]]

    def apply(self, conn, step, answers):
        return run.Outcome()


@settings(max_examples=50, deadline=None)
@given(
    costs=st.lists(
        st.tuples(st.integers(0, 500), st.integers(0, 500)), min_size=1, max_size=12
    ),
    limit=st.integers(1, 3000),
)
def test_tokens_used_is_the_sum_of_recorded_calls_and_no_call_starts_past_the_limit(
    costs, limit
):
    conn = store.open_store(Path(":memory:"))
    run_id = add_run(conn, cfg=fake_config(tokens=limit))
    clock = FakeClock()
    model = FakeModel(clock)
    priced = iter(costs * 2)

    def price(role, prompt):
        model.tokens = next(priced)

    model.before = price
    planner = reads(len(costs), review=False)
    runner = make_runner(
        conn,
        Path("/nonexistent"),
        run_id,
        fake_config(tokens=limit),
        phases={"read": Retrying()},
        model=model,
        clock=clock,
        planner=planner,
    )
    runner.execute()

    rows = conn.execute(
        "SELECT tokens_in, tokens_out FROM calls ORDER BY id"
    ).fetchall()
    assert budget.tokens_used(conn, run_id) == sum(r[0] + r[1] for r in rows)
    spent = 0
    for row in rows:
        # The read share (1 - review_author_reserve) caps Read calls.
        assert spent < limit * 0.7
        spent += row[0] + row[1]
    conn.close()


def test_reaching_the_token_budget_ends_the_run_budget_exhausted(db, home, run_id):
    cfg = fake_config(tokens=300, review_author_reserve=0.0)
    reports = []
    clock = FakeClock()
    model = FakeModel(clock, tokens=(100, 20))

    def report(conn, run):
        reports.append((run, len(model.prompts)))

    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader"), "review": CallPhase("reviewer")},
        model=model,
        clock=clock,
        planner=reads(4),
        report=report,
    )
    status = runner.execute()
    assert status == "budget_exhausted"
    assert run.exit_code(status) == 0
    assert len(model.prompts) == 3  # 360 tokens: the third call crossed 300
    assert step_states(db) == {
        "read:1": "done",
        "read:2": "done",
        "read:3": "done",
        "read:4": "skipped",
        "review": "skipped",
    }
    reasons = {s.key: s.reason for s in steps.load(db, run_id)}
    assert "token budget" in reasons["review"]
    # The report ran once, after the last call, with no model call of its own.
    assert reports == [(run_id, 3)]
    row = run_row(db)
    assert row["status"] == "budget_exhausted"
    assert row["ended_at"] is not None


def test_a_call_that_crosses_the_budget_is_not_cut_off(db, home, run_id):
    cfg = fake_config(tokens=100, review_author_reserve=0.0)
    clock = FakeClock()
    model = FakeModel(clock, tokens=(400, 50))
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader")},
        model=model,
        clock=clock,
        planner=reads(2, review=False),
    )
    assert runner.execute() == "budget_exhausted"
    # Limits are checked between steps and before calls, never during one.
    assert step_states(db) == {"read:1": "done", "read:2": "skipped"}
    assert results(db) == {"read:1": "[0]"}


def test_no_further_call_starts_inside_a_step_once_the_limit_is_reached(
    db, home, run_id
):
    cfg = fake_config(tokens=100, review_author_reserve=0.0)
    clock = FakeClock()
    model = FakeModel(clock, tokens=(400, 50))
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader", parts=("one", "two"))},
        model=model,
        clock=clock,
        planner=reads(1, review=False),
    )
    assert runner.execute() == "budget_exhausted"
    assert len(model.prompts) == 1
    assert step_states(db) == {"read:1": "skipped"}


def test_steps_without_model_calls_continue_after_the_budget_is_exhausted(
    db, home, run_id
):
    cfg = fake_config(tokens=100, review_author_reserve=0.0)
    clock = FakeClock()

    def planner(ctx):
        return [
            NewStep("author:1", "author"),
            NewStep("author:2", "author"),
            NewStep("adopt:1", "adopt", depends_on=("author:1", "author:2")),
        ]

    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"author": CallPhase("author"), "adopt": LocalPhase()},
        model=FakeModel(clock, tokens=(400, 50)),
        clock=clock,
        planner=planner,
    )
    assert runner.execute() == "budget_exhausted"
    assert step_states(db) == {
        "author:1": "done",
        "author:2": "skipped",
        "adopt:1": "done",
    }


def test_read_steps_stop_at_the_read_share_and_review_still_runs(db, home, run_id):
    # 1000 tokens with 40% reserved: Read calls may start below 600.
    cfg = fake_config(tokens=1000, review_author_reserve=0.4)
    clock = FakeClock()
    model = FakeModel(clock, tokens=(250, 0))
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader"), "review": CallPhase("reviewer")},
        model=model,
        clock=clock,
        planner=reads(5),
    )
    assert runner.execute() == "complete"
    assert step_states(db) == {
        "read:1": "done",
        "read:2": "done",
        "read:3": "done",
        "read:4": "skipped",
        "read:5": "skipped",
        "review": "done",
    }
    reasons = {s.key: s.reason for s in steps.load(db, run_id)}
    assert "reserved" in reasons["read:4"]


# Wall-clock time


def test_wall_clock_limit_ends_the_run_budget_exhausted(db, home, run_id):
    cfg = fake_config(wall_clock_minutes=1)
    clock = FakeClock()
    model = FakeModel(clock, seconds=40)
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader")},
        model=model,
        clock=clock,
        planner=reads(3, review=False),
    )
    assert runner.execute() == "budget_exhausted"
    assert len(model.prompts) == 2
    reasons = {s.key: s.reason for s in steps.load(db, run_id)}
    assert "wall-clock" in reasons["read:3"]
    assert run_row(db)["active_seconds"] == pytest.approx(80)


class Waiting:
    """A call that meets a usage limit, waits for its reset and is made again."""

    def __init__(self, hours):
        self.hours = hours

    def prepare(self, ctx, step):
        until = ctx.clock.now() + timedelta(hours=self.hours)
        ctx.wait_until(until, {"backend": "fake", "reset": until.isoformat()})
        return ctx.call("reader", step.key, SCHEMA)["value"]

    def apply(self, conn, step, answer):
        return run.Outcome()


def test_wall_clock_counts_only_while_running_not_while_waiting(db, home, run_id):
    cfg = fake_config(wall_clock_minutes=10)
    clock = FakeClock()
    seen = []

    def on_sleep(seconds):
        row = run_row(db)
        seen.append((row["status"], row["wait_until"], row["wait_notice"]))

    clock.on_sleep = on_sleep
    model = FakeModel(clock, seconds=60)
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": Waiting(hours=3)},
        model=model,
        clock=clock,
        planner=reads(2, review=False),
    )
    assert runner.execute() == "complete"
    assert len(model.prompts) == 2
    assert run_row(db)["active_seconds"] == pytest.approx(120)
    # While asleep the run is `waiting` with the reset time and notice stored.
    status, until, notice = seen[0]
    assert status == "waiting"
    assert until.startswith("2026-11-15T12:00:00")
    assert '"backend": "fake"' in notice
    row = run_row(db)
    assert (row["status"], row["wait_until"], row["wait_notice"]) == (
        "complete",
        None,
        None,
    )


def test_an_interrupt_while_waiting_stops_the_run(db, home, run_id):
    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": Waiting(hours=3)},
        model=FakeModel(clock),
        clock=clock,
        planner=reads(1, review=False),
    )
    clock.on_sleep = lambda seconds: runner.stop()
    assert runner.execute() == "interrupted"
    assert step_states(db) == {"read:1": "running"}
    assert run_row(db)["status"] == "interrupted"


def test_resume_keeps_the_remaining_budget(db, home, run_id):
    cfg = fake_config(tokens=300, review_author_reserve=0.0, wall_clock_minutes=10)
    clock = FakeClock()
    model = FakeModel(clock, tokens=(100, 0), seconds=100)
    phases = {"read": CallPhase("reader")}
    first = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases=phases,
        model=model,
        clock=clock,
        planner=reads(5, review=False),
    )
    calls = []

    class StopAfterTwo(CallPhase):
        def apply(self, conn, step, answers):
            calls.append(step.key)
            if len(calls) == 2:
                first.stop()
            return super().apply(conn, step, answers)

    first.phases = {"read": StopAfterTwo("reader")}
    assert first.execute() == "interrupted"
    assert run_row(db)["active_seconds"] == pytest.approx(200)

    again = make_runner(db, home, run_id, cfg, phases=phases, model=model, clock=clock)
    assert again.execute() == "budget_exhausted"
    # One more call fits in the 300 tokens left over from the first part.
    assert len(model.prompts) == 3
    assert budget.tokens_used(db, run_id) == 300
    assert run_row(db)["active_seconds"] == pytest.approx(300)


def test_usage_per_backend_and_model(db, home, run_id):
    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": CallPhase("reader")},
        model=FakeModel(clock, tokens=(100, 20), seconds=5),
        clock=clock,
        planner=reads(2, review=False),
    )
    runner.execute()
    assert budget.usage_by_backend(db, run_id) == [
        {
            "backend": "fake",
            "model": "fake-model",
            "calls": 2,
            "tokens_in": 200,
            "tokens_out": 40,
            "seconds": pytest.approx(10),
        }
    ]

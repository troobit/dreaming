import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from dreaming.budget import RunBudget
from dreaming.config import Budget
from dreaming.steps import CallResult, Step, Steps
from dreaming.store import Store


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


@settings(max_examples=20)
@given(st.lists(st.tuples(st.integers(0, 100), st.integers(0, 100)), max_size=10))
def test_accounting_is_sum_of_all_attempts(calls):
    with tempfile.TemporaryDirectory() as directory:
        with Store(Path(directory) / "home") as store:
            store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
            steps = Steps(store, "r")
            steps.add(Step("read:a", "Read"))
            steps.start("read:a")
            for incoming, outgoing in calls:
                ref = steps.begin_call("read:a", role="reader", backend="fake")
                steps.record_call(ref, CallResult("error", incoming, outgoing, outcome="error"))
            budget = RunBudget(store, "r", Budget())
            assert budget.tokens_used == sum(a + b for a, b in calls)


def test_wait_and_downtime_do_not_consume_active_seconds(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        clock = Clock()
        budget = RunBudget(store, "r", Budget(max_seconds=20), clock=clock)
        clock.value = 5
        budget.pause("tomorrow", "provider reset")
        clock.value = 5000
        assert budget.seconds_used == 5
        assert store.connection.execute("SELECT status,wait_until FROM runs").fetchone() == (
            "waiting",
            "tomorrow",
        )
        budget.resume()
        clock.value += 4
        budget.checkpoint()
        assert budget.seconds_used == 9
        clock.value += 1000
        resumed = RunBudget(store, "r", Budget(max_seconds=20), clock=clock)
        assert resumed.seconds_used == 9
        clock.value += 11
        assert resumed.exhausted()
        assert (
            json.loads(store.connection.execute("SELECT statistics FROM runs").fetchone()[0])[
                "active_seconds"
            ]
            == 9
        )


def test_reader_reserve_does_not_stop_review(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        steps = Steps(store, "r")
        steps.add(Step("a", "Read"))
        steps.start("a")
        ref = steps.begin_call("a", role="reader", backend="fake")
        steps.record_call(ref, CallResult("{}", 75, 0))
        budget = RunBudget(store, "r", Budget(max_tokens=100, reserve_share=0.25))
        assert not budget.can_call("reader")
        assert budget.can_call("reviewer")

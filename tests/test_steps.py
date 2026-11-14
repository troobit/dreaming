import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dreaming.steps import CallResult, Step, Steps
from dreaming.store import Store


def setup_steps(store):
    store.connection.execute("INSERT INTO runs(id,status) VALUES ('run','running')")
    steps = Steps(store, "run")
    steps.add(Step("read:a", "Read"), Step("review", "Review", ("read:a",)))
    return steps


def test_dependencies_and_atomic_followups(tmp_path):
    with Store(tmp_path / "home") as store:
        steps = setup_steps(store)
        assert steps.next().key == "read:a"
        steps.start("read:a")
        call = steps.begin_call("read:a", role="reader", backend="fake", model="m")
        steps.record_call(call, CallResult('{"answer":1}', 2, 3))

        def broken(conn, output):
            conn.execute("UPDATE runs SET notice=?", (output,))
            raise RuntimeError("crash in application")

        with pytest.raises(RuntimeError):
            steps.apply("read:a", '{"answer":1}', broken, (Step("research:q", "Research"),))
        assert store.connection.execute("SELECT notice FROM runs").fetchone()[0] is None
        assert len(steps.list()) == 2
        steps.apply("read:a", '{"answer":1}', lambda conn, raw: None)
        assert steps.next().key == "review"
        assert steps.saved_output("read:a") == '{"answer":1}'
        assert store.connection.execute("SELECT attempt,model FROM calls").fetchone() == (1, "m")


def test_followup_insertion_failure_rolls_back_applied_evidence(tmp_path):
    with Store(tmp_path / "home") as store:
        steps = setup_steps(store)
        steps.start("read:a")

        def apply(conn, raw):
            conn.execute("UPDATE runs SET notice='applied'")
            return (Step("review", "Review"),)

        with pytest.raises(ValueError, match="already"):
            steps.apply("read:a", "{}", apply)
        assert store.connection.execute("SELECT notice FROM runs").fetchone() == (None,)
        assert steps.list()[0]["state"] == "running"
        steps.apply(
            "read:a", "{}", lambda conn, raw: (Step("research:q", "Research", ("read:a",)),)
        )
        assert steps.list()[-1]["key"] == "research:q"


def test_rejects_cycles_missing_dependencies_and_changed_keys(tmp_path):
    with Store(tmp_path / "home") as store:
        steps = setup_steps(store)
        with pytest.raises(ValueError, match="depend"):
            steps.add(Step("invalid", "Read", ("missing",)))
        with pytest.raises(ValueError, match="cycl"):
            steps.add(Step("a", "Read", ("b",)), Step("b", "Read", ("a",)))
        with pytest.raises(ValueError, match="already"):
            steps.add(Step("read:a", "Different"))
        assert len(steps.list()) == 2


@settings(max_examples=20)
@given(st.sampled_from(["before_call", "after_raw", "during_apply", "after_commit"]))
def test_resume_uses_saved_answer_at_every_durable_boundary(crash):
    import tempfile
    from pathlib import Path

    from dreaming.config import Budget
    from dreaming.run import Engine

    with tempfile.TemporaryDirectory() as directory:
        with Store(Path(directory) / "home") as store:
            steps = setup_steps(store)
            store.connection.execute("UPDATE runs SET budget=?", (json.dumps(Budget().__dict__),))
            answers = []

            def call():
                answers.append(len(answers) + 1)
                return CallResult(json.dumps({"answer": answers[-1]}), 2, 3)

            class Handler:
                def prepare(self, step):
                    return {"role": "reader", "backend": "fake"}

                def call(self, step):
                    return call()

                def apply(self, conn, step, raw):
                    conn.execute("UPDATE runs SET notice=?", (raw,))
                    return ()

            handler = Handler()
            if crash != "before_call":
                steps.start("read:a")
                ref = steps.begin_call("read:a", role="reader", backend="fake")
                response = call()
                steps.record_call(ref, response)
                if crash == "after_commit":
                    steps.apply("read:a", response.raw_output, lambda c, r: None)
                elif crash == "during_apply":

                    def broken(conn, raw):
                        conn.execute("UPDATE runs SET notice='wrong'")
                        raise RuntimeError()

                    with pytest.raises(RuntimeError):
                        steps.apply("read:a", response.raw_output, broken)

            # The second phase is deterministic and makes no model call.
            class Review:
                def apply(self, conn, step, raw):
                    return ()

            engine = Engine(store, "run", {"Read": handler, "Review": Review()})
            assert engine.execute() == 0
            assert answers == [1]
            assert all(s["state"] == "done" for s in steps.list())
            assert json.loads(steps.list()[0]["result"]) == {"answer": 1}


def test_indeterminate_call_stops_without_issuing_another_answer(tmp_path):
    from dreaming.run import Engine

    with Store(tmp_path / "home") as store:
        steps = setup_steps(store)
        steps.start("read:a")
        steps.begin_call("read:a", role="reader", backend="fake")
        assert Engine(store, "run", {}).execute() == 5
        assert store.connection.execute("SELECT status,notice FROM runs").fetchone() == (
            "partial",
            "indeterminate call for read:a; no durable response",
        )

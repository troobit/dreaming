import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_dispatcher import FakeBackend
from test_observe import observation, selected_session

from dreaming.backends import Dispatcher
from dreaming.config import BackendConfig, load_config
from dreaming.reader import ReadHandler, clusters, schedule_reads
from dreaming.roles import RoleSet
from dreaming.run import Engine
from dreaming.sessions import Selection
from dreaming.steps import CallResult, Steps
from dreaming.store import Store

INSTRUCTIONS = Path(__file__).resolve().parents[1] / "instructions"


@pytest.fixture
def env(tmp_path):
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        backends={"fake": BackendConfig(context_tokens=12000)},
        roles={r: ("fake",) for r in config.roles},
    )
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        selected = selected_session(store, config, "Prefer Make sk-" + "a" * 30)
        yield store, config, RoleSet(INSTRUCTIONS, config), selected


class NoReview:
    def apply(self, conn, step, raw):
        return ()


def run_reader(env, backend, handler=None):
    store, config, roles, selected = env
    handler = handler or ReadHandler(store, "r", config, roles, (selected,))
    return Engine(
        store,
        "r",
        {"Read": handler, "Review": NoReview()},
        dispatcher=Dispatcher(store, "r", config, {"fake": backend}),
    ).execute()


def test_read_dispatches_verifies_before_redaction_and_saves_cursor(env):
    store, config, roles, selected = env
    schedule_reads(store, "r", Selection((selected,), (), ()))
    raw = observation()
    raw["quote"] = "Prefer Make sk-" + "a" * 30
    backend = FakeBackend(
        "fake", [CallResult(json.dumps({"observations": [raw]}), model="reader-m")]
    )
    assert run_reader(env, backend) == 0
    assert store.connection.execute("SELECT model FROM observations").fetchone()[0] == "reader-m"
    saved = store.connection.execute("SELECT raw_output FROM calls").fetchone()[0]
    assert "a" * 30 not in saved and json.loads(saved)["gate_version"] == 1
    assert store.connection.execute("SELECT entries_read FROM sessions_seen").fetchone()[0] == 1
    assert len(backend.prompts) == 1


def test_crash_after_checkpoint_applies_same_output_without_call(env):
    store, config, roles, selected = env
    schedule_reads(store, "r", Selection((selected,), (), ()))
    handler = ReadHandler(store, "r", config, roles, (selected,))
    original = handler.apply
    handler.apply = lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    backend = FakeBackend("fake", [CallResult(json.dumps({"observations": [observation()]}))])
    with pytest.raises(RuntimeError, match="crash"):
        run_reader(env, backend, handler)
    handler.apply = original
    assert run_reader(env, FakeBackend("fake", []), handler) == 0
    assert store.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1


def test_bad_schema_is_reported_and_unchanged_input_not_reselected(env):
    store, config, roles, selected = env
    schedule_reads(store, "r", Selection((selected,), (), ()))
    assert run_reader(env, FakeBackend("fake", [CallResult("{}"), CallResult("{}")])) == 0
    assert store.connection.execute("SELECT state FROM sessions_seen").fetchone()[0] == "failed"
    assert not store.connection.execute("SELECT * FROM observations").fetchall()
    assert json.loads(Steps(store, "r").list()[0]["result"])["omitted"] == "schema_failure"
    from test_selection import Source, select

    assert not select(Source(selected.document), (config, store)).sessions


def test_split_parts_resize_after_loaded_context_changes_and_review_waits_for_all(env):
    from dreaming.sessions import ContentBlock, Entry

    store, config, roles, selected = env
    selected = replace(
        selected,
        document=replace(
            selected.document,
            entries=tuple(
                Entry("user", (ContentBlock("text", {"text": "a" * 10000}),)) for _ in range(3)
            ),
        ),
        copy_entry_counts=(3,),
    )
    env = store, config, roles, selected
    schedule_reads(store, "r", Selection((selected,), (), ()))

    class ShrinkingBackend:
        name = "fake"
        supports_tools = False
        context_tokens = 12000
        calls = 0

        def call(self, role, prompt, schema, tools):
            self.calls += 1
            if self.calls == 1:
                self.context_tokens = 6000
                return CallResult("", outcome="context_changed")
            return CallResult('{"observations": []}', 1, 1)

    backend = ShrinkingBackend()
    assert run_reader(env, backend) == 0
    assert backend.calls > 2
    assert all(s["state"] == "done" for s in Steps(store, "r").list())
    assert store.connection.execute("SELECT entries_read FROM sessions_seen").fetchone()[0] == 3
    assert len(json.loads(Steps(store, "r").list()[1]["depends_on"])) >= 3


def test_forgetting_before_prepare_makes_no_reader_call(env):
    from dreaming.knowledge import forget_session

    store, _, _, selected = env
    schedule_reads(store, "r", Selection((selected,), (), ()))
    forget_session(store, "claude", "s")
    backend = FakeBackend("fake", [])
    assert run_reader(env, backend) == 0
    assert not backend.prompts


def test_forgetting_during_call_discards_output_without_retry(env):
    from dreaming.knowledge import forget_session

    store, _, _, selected = env
    schedule_reads(store, "r", Selection((selected,), (), ()))

    class ForgettingBackend(FakeBackend):
        def call(self, *args):
            forget_session(store, "claude", "s")
            return super().call(*args)

    backend = ForgettingBackend("fake", [CallResult(json.dumps({"observations": [observation()]}))])
    assert run_reader(env, backend) == 0
    assert len(backend.prompts) == 1
    assert not store.connection.execute("SELECT * FROM observations").fetchall()
    assert store.connection.execute("SELECT outcome FROM calls").fetchone()[0] == "input_rejected"


@given(st.permutations(["a", "b", "c"]))
def test_clustering_order_is_deterministic(order):
    rows = [
        dict(
            id=i,
            pattern_id="p",
            pattern_key="preferences:make",
            lens_id="preferences",
            observed_at="2026-11-10",
            redacted_quote=i,
        )
        for i in order
    ]
    assert clusters(rows) == clusters(list(reversed(rows)))

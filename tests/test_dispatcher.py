import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from dreaming.backends import Dispatcher, usage_reset
from dreaming.config import BackendConfig, Thresholds, load_config
from dreaming.steps import CallResult, Step, Steps
from dreaming.store import Store

SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


class FakeBackend:
    supports_tools = True

    def __init__(self, name, answers):
        self.name, self.answers, self.prompts = name, iter(answers), []

    def call(self, role, prompt, schema, tools):
        self.prompts.append(prompt)
        return next(self.answers)


@pytest.fixture
def dispatch(tmp_path):
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        roles={**config.roles, "reader": ("a", "b")},
        backends={"a": BackendConfig(context_tokens=2048), "b": BackendConfig(context_tokens=1024)},
    )
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        steps = Steps(store, "r")
        steps.add(Step("read", "Read"))
        steps.start("read")
        yield store, config


def error(text="offline"):
    return CallResult(text, outcome="transport_failure", error=text)


def test_transport_retry_fallback_records_every_attempt_before_dispatch(dispatch):
    store, config = dispatch
    a, b = (
        FakeBackend("a", [error(), error()]),
        FakeBackend("b", [error(), CallResult('{"ok":true}')]),
    )
    original = b.call

    def call(*args):
        assert (
            store.connection.execute("SELECT COUNT(*) FROM calls WHERE outcome IS NULL").fetchone()[
                0
            ]
            == 1
        )
        return original(*args)

    b.call = call
    d = Dispatcher(store, "r", config, {"a": a, "b": b})
    result = d.dispatch("read", "reader", "hello", SCHEMA, [])
    assert result.outcome == "success"
    rows = store.connection.execute(
        "SELECT backend,attempt,usage_estimated FROM calls ORDER BY attempt"
    ).fetchall()
    assert rows == [("a", 1, 1), ("a", 2, 1), ("b", 3, 1), ("b", 4, 1)]
    assert d.unavailable == {"a"}
    assert d.budget.tokens_used > 0


def test_schema_failure_stops_input_without_transport_fallback(dispatch):
    store, config = dispatch
    a, b = FakeBackend("a", [CallResult("{}"), CallResult("bad")]), FakeBackend("b", [])
    result = Dispatcher(store, "r", config, {"a": a, "b": b}).dispatch(
        "read", "reader", "x", SCHEMA, []
    )
    assert result.outcome == "schema_failure"
    assert not b.prompts
    assert store.connection.execute("SELECT state FROM steps").fetchone()[0] == "failed"


def test_breaker_moves_input_to_fallback_and_unavailable_survives_dispatcher(dispatch):
    store, config = dispatch
    config = replace(config, thresholds=replace(config.thresholds, breaker_k=2))
    a, b = (
        FakeBackend("a", [CallResult("{}"), CallResult("{}")]),
        FakeBackend("b", [CallResult('{"ok":true}')]),
    )
    d = Dispatcher(store, "r", config, {"a": a, "b": b})
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "success"
    assert Dispatcher(store, "r", config, {}).unavailable == {"a"}
    assert not Dispatcher(store, "r", config, {}, resumed=True).unavailable


@pytest.mark.parametrize(
    "text,expected",
    [
        ("You've hit your usage limit. Try again at 4:18 PM.", "2026-11-15T16:18:00+00:00"),
        (
            "You've hit your usage limit. Try again at Nov 16th, 2026 1:39 AM.",
            "2026-11-16T01:39:00+00:00",
        ),
        (
            "You've hit your usage limit. Try again at November 16, 2026 1:39 AM.",
            "2026-11-16T01:39:00+00:00",
        ),
        ("You've reached your session limit. Resets at 5pm (UTC)", "2026-11-15T17:00:00+00:00"),
        ("You've hit your limit · resets 5pm (UTC)", "2026-11-15T17:00:00+00:00"),
        ("Usage limit reached. Resets at Nov 16, 2026 5:30 AM (UTC)", "2026-11-16T05:30:00+00:00"),
        ("You've hit your limit · resets 5am (Australia/Melbourne)", "2026-11-15T18:00:00+00:00"),
        ("You've hit your usage limit. Try again at 2 AM.", "2026-11-16T02:00:00+00:00"),
        ("Try again at 4:18 PM", None),
        ("You've hit your usage limit.", None),
        ("Usage limit resets at 25:80 PM", None),
        ("Usage limit resets at 5pm (Invented/Timezone)", None),
    ],
)
def test_usage_reset_known_messages(text, expected):
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    result = usage_reset(text, now)
    assert (result.astimezone(UTC).isoformat() if result else None) == expected


def test_wait_is_not_retry_or_active_time_and_notice_is_durable(dispatch):
    store, config = dispatch
    now = [datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp()]
    config = replace(config, thresholds=replace(config.thresholds, max_usage_wait=120))
    a = FakeBackend(
        "a",
        [error("You've hit your usage limit. Try again at 12:01 PM."), CallResult('{"ok":true}')],
    )
    notices = []

    def sleep(seconds):
        status, until, notice = store.connection.execute(
            "SELECT status,wait_until,notice FROM runs"
        ).fetchone()
        assert status == "waiting" and until
        notices.append(notice)
        now[0] += seconds

    d = Dispatcher(
        store,
        "r",
        config,
        {"a": a},
        wall_clock=lambda: datetime.fromtimestamp(now[0], UTC),
        clock=lambda: now[0],
        sleep=sleep,
    )
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "success"
    assert d.budget.seconds_used == 0
    assert len(a.prompts) == 2 and not d.unavailable
    assert "fallback_on_usage_limit" in notices[0] and "2048" in notices[0] and "1024" in notices[0]
    assert store.connection.execute("SELECT status,wait_until FROM runs").fetchone() == (
        "running",
        None,
    )


def test_usage_limit_option_falls_back_immediately(dispatch):
    store, config = dispatch
    config = replace(config, thresholds=Thresholds(fallback_on_usage_limit=True))
    a, b = (
        FakeBackend("a", [error("You've hit your usage limit. Try again at 4:18 PM.")]),
        FakeBackend("b", [CallResult('{"ok":true}')]),
    )
    assert (
        Dispatcher(store, "r", config, {"a": a, "b": b})
        .dispatch("read", "reader", "x", SCHEMA, [])
        .outcome
        == "success"
    )
    assert len(a.prompts) == 1


def test_no_backend_and_budget_stop_are_distinct(dispatch):
    store, config = dispatch
    d = Dispatcher(store, "r", config, {"a": FakeBackend("a", [error(), error()])})
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "partial"
    assert store.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 2
    assert json.loads(store.connection.execute("SELECT statistics FROM runs").fetchone()[0])[
        "backend_unavailable"
    ]


def test_oversized_prompt_is_rejected_before_model_call(dispatch):
    store, config = dispatch
    a = FakeBackend("a", [])
    with pytest.raises(ValueError, match="context"):
        Dispatcher(store, "r", config, {"a": a}).dispatch("read", "reader", "x" * 10000, SCHEMA, [])
    assert not a.prompts


@pytest.mark.parametrize(
    "text",
    [
        "unknown provider message",
        "You've hit your usage limit.",
        "You've hit your usage limit. Try again at Nov 17, 2026 1 AM.",
    ],
)
def test_unusable_usage_reset_is_a_transport_failure(dispatch, text):
    store, config = dispatch
    a = FakeBackend("a", [error(text), error(text)])
    d = Dispatcher(
        store, "r", config, {"a": a}, wall_clock=lambda: datetime(2026, 11, 15, 12, tzinfo=UTC)
    )
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "partial"
    assert len(a.prompts) == 2 and "a" in d.unavailable


def test_interrupt_during_wait_prevents_repeat_call(dispatch):
    store, config = dispatch
    stopped = []
    a = FakeBackend("a", [error("You've hit your usage limit. Try again at 12:01 PM.")])
    d = Dispatcher(
        store,
        "r",
        config,
        {"a": a},
        wall_clock=lambda: datetime(2026, 11, 15, 12, tzinfo=UTC),
        sleep=lambda seconds: stopped.append(True),
        interrupted=lambda: bool(stopped),
    )
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "interrupted"
    assert len(a.prompts) == 1
    assert store.connection.execute("SELECT wait_until FROM runs").fetchone()[0]


def test_dispatcher_runs_through_engine_and_reuses_saved_answer(dispatch):
    from dreaming.run import Engine

    store, config = dispatch
    a = FakeBackend("a", [error(), CallResult('{"ok":true}', 7, 3)])
    applied = []

    class Handler:
        def prepare(self, step):
            return dict(role="reader", prompt="hi", schema=SCHEMA, tools=[])

        def apply(self, conn, step, raw):
            applied.append(json.loads(raw))

    d = Dispatcher(store, "r", config, {"a": a})
    assert Engine(store, "r", {"Read": Handler()}, dispatcher=d).execute() == 0
    assert applied == [{"ok": True}]
    assert len(a.prompts) == 2


def test_retry_stops_when_own_budget_is_exhausted(dispatch):
    store, config = dispatch
    config = replace(config, budget=replace(config.budget, max_tokens=10))
    a = FakeBackend("a", [CallResult("offline", 8, 8, outcome="transport_failure")])
    d = Dispatcher(store, "r", config, {"a": a})
    assert d.dispatch("read", "reader", "x", SCHEMA, []).outcome == "budget_exhausted"
    assert len(a.prompts) == 1


def test_breaker_requeues_earlier_schema_failed_inputs(dispatch):
    store, config = dispatch
    a = FakeBackend("a", [CallResult("{}"), CallResult("{}"), CallResult("{}")])
    b = FakeBackend("b", [CallResult('{"ok":true}'), CallResult('{"ok":true}')])
    d = Dispatcher(store, "r", config, {"a": a, "b": b})
    assert d.dispatch("read", "reader", "one", SCHEMA, []).outcome == "schema_failure"
    d.steps.add(Step("read2", "Read"))
    d.steps.start("read2")
    assert d.dispatch("read2", "reader", "two", SCHEMA, []).outcome == "success"
    assert (
        store.connection.execute("SELECT state FROM steps WHERE key='read'").fetchone()[0]
        == "pending"
    )
    d.steps.start("read")
    assert d.dispatch("read", "reader", "one", SCHEMA, []).outcome == "success"
    assert b.prompts == ["two", "one"]


def test_saved_response_is_reused_even_after_context_shrinks(dispatch):
    store, config = dispatch
    d = Dispatcher(store, "r", config, {"a": FakeBackend("a", [CallResult('{"ok":true}')])})
    assert d.dispatch("read", "reader", "one", SCHEMA, []).outcome == "success"
    config = replace(
        config,
        backends={"a": BackendConfig(context_tokens=1), "b": BackendConfig(context_tokens=1)},
    )
    assert (
        Dispatcher(store, "r", config, {}).dispatch("read", "reader", "one", SCHEMA, []).raw_output
        == '{"ok":true}'
    )


def test_schema_failed_session_is_not_reselected_implicitly(dispatch):
    store, config = dispatch
    store.connection.execute("INSERT INTO sessions_seen(source,session_id) VALUES ('claude','s')")
    d = Dispatcher(
        store, "r", config, {"a": FakeBackend("a", [CallResult("{}"), CallResult("{}")])}
    )
    assert (
        d.dispatch("read", "reader", "one", SCHEMA, [], sessions=(("claude", "s"),)).outcome
        == "schema_failure"
    )
    assert store.connection.execute("SELECT state FROM sessions_seen").fetchone()[0] == "failed"


def test_reported_model_and_usage_are_saved_without_estimation(dispatch):
    store, config = dispatch
    a = FakeBackend("a", [CallResult('{"ok":true}', 17, 4, model="reported-model")])
    Dispatcher(store, "r", config, {"a": a}).dispatch("read", "reader", "x", SCHEMA, [])
    assert store.connection.execute(
        "SELECT model,tokens_in,tokens_out,usage_estimated FROM calls"
    ).fetchone() == ("reported-model", 17, 4, 0)


def test_engine_requeues_failed_omissions_before_dependent_review(dispatch):
    from dreaming.run import Engine

    store, config = dispatch
    steps = Steps(store, "r")
    steps.add(Step("read2", "Read"), Step("review", "Review", ("read", "read2")))
    a = FakeBackend("a", [CallResult("{}"), CallResult("{}"), CallResult("{}")])
    b = FakeBackend("b", [CallResult('{"ok":true}'), CallResult('{"ok":true}')])
    applied = []

    class Reader:
        def prepare(self, step):
            return dict(role="reader", prompt=step.key, schema=SCHEMA, tools=[])

        def apply(self, conn, step, raw):
            applied.append(step.key)

    class Review:
        def apply(self, conn, step, raw):
            applied.append("review")

    d = Dispatcher(store, "r", config, {"a": a, "b": b})
    assert Engine(store, "r", {"Read": Reader(), "Review": Review()}, dispatcher=d).execute() == 0
    assert applied == ["read2", "read", "review"]


def test_engine_continues_review_after_a_schema_failed_input(dispatch):
    from dreaming.run import Engine

    store, config = dispatch
    steps = Steps(store, "r")
    steps.add(Step("review", "Review", ("read",)))
    reviewed = []

    class Reader:
        def prepare(self, step):
            return dict(role="reader", prompt="x", schema=SCHEMA, tools=[])

        def apply(self, conn, step, raw):
            pytest.fail("a schema-invalid result must never be applied")

    class Review:
        def apply(self, conn, step, raw):
            reviewed.append(True)

    d = Dispatcher(
        store, "r", config, {"a": FakeBackend("a", [CallResult("{}"), CallResult("{}")])}
    )
    assert Engine(store, "r", {"Read": Reader(), "Review": Review()}, dispatcher=d).execute() == 0
    assert reviewed
    assert store.connection.execute(
        "SELECT result,reason FROM steps WHERE key='read'"
    ).fetchone() == ('{"omitted":"schema_failure"}', "role schema failed twice")


def test_unavailable_probe_does_not_degrade_a_successful_fallback_run(dispatch):
    from dreaming.backends.probe import ProbeResult
    from dreaming.run import Engine

    store, config = dispatch
    a, b = FakeBackend("a", []), FakeBackend("b", [CallResult('{"ok":true}')])

    def probe():
        store.connection.execute(
            "INSERT INTO steps(run_id,key,phase,state) VALUES ('r','probe:a','Probe','failed')"
        )
        return ProbeResult("a", "claude", reason="unavailable")

    a.probe = probe

    class Reader:
        def prepare(self, step):
            return dict(role="reader", prompt="x", schema=SCHEMA, tools=[])

        def apply(self, conn, step, raw):
            pass

    d = Dispatcher(store, "r", config, {"a": a, "b": b})
    assert Engine(store, "r", {"Read": Reader()}, dispatcher=d).execute() == 0

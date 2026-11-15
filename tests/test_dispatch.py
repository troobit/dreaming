"""Retry, fallback, circuit breaker and usage-limit waits (4.7, 5.2, 5.3, 5.6,
Decision 16), driven through the runner with scripted fake backends."""

import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from runkit import CallPhase, FakeClock, add_run, reads, results, run_row, step_states

from dreaming import config, roles, run, steps
from dreaming.backends import (
    BackendError,
    CallResult,
    estimate_tokens,
    run_process,
    usage_limit_reset,
)
from dreaming.errors import PARTIAL

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
CLAUDE_LIMIT = "You've hit your limit · resets 1pm (UTC)"


class Scripted:
    """A backend answering from a script. Each item is an output, a CallResult or
    an exception to raise; once the script is used up it answers `{"value": n}`."""

    accepts_session_id = False

    def __init__(self, name, script=(), *, context_tokens=200_000, release_date=""):
        self.name = name
        self.script = list(script)
        self.context_tokens = context_tokens
        self.release_date = release_date
        self.prompts = []

    def call(self, role, prompt, schema, tools, session_id=None, workdir=None):
        self.prompts.append(prompt)
        item = self.script.pop(0) if self.script else {"value": len(self.prompts)}
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, CallResult):
            return item
        return CallResult(item, 100, 20, session_ref=f"{self.name}-session")


def two_backends(**thresholds):
    return config.build_config(
        {
            "backends": {
                "a": {
                    "harness": "claude",
                    "context_tokens": 200_000,
                    "release_date": "2026-05-01",
                },
                "b": {"harness": "codex", "context_tokens": 32_768},
            },
            "roles": {role: {"backends": ["a", "b"]} for role in config.ROLES},
            "thresholds": thresholds,
        }
    )


class Strict(CallPhase):
    """CallPhase with a schema that rejects anything but `{"value": <int>}`."""

    def prepare(self, ctx, step):
        return [ctx.call(self.role, step.key, SCHEMA)["value"]]


@pytest.fixture
def dream(db, home):
    """Runs `count` Read steps against backends `a` and `b`."""

    def go(a, b, count=1, clock=None, **thresholds):
        cfg = two_backends(**thresholds)
        run_id = add_run(db, cfg=cfg)
        runner = run.Runner(
            db,
            home,
            run_id,
            cfg,
            phases={"read": Strict("reader")},
            backends={"a": a, "b": b},
            planner=reads(count, review=False),
            clock=clock or FakeClock(),
        )
        return runner.execute()

    return go


def calls(db):
    return [
        (row["backend"], row["outcome"])
        for row in db.execute("SELECT * FROM calls ORDER BY id")
    ]


def step_reasons(db):
    return {s.key: s.reason for s in steps.load(db, "dream-test")}


# Schema failures (4.7)


def test_output_that_misses_the_schema_is_retried_once_on_the_same_backend(dream, db):
    a, b = Scripted("a", [{"wrong": 1}, {"value": 7}]), Scripted("b")
    assert dream(a, b) == "complete"
    assert results(db) == {"read:1": "[7]"}
    assert calls(db) == [("a", "invalid"), ("a", "ok")]
    assert b.prompts == []
    # The rejected output is kept with what was wrong, and its tokens count.
    rejected = db.execute("SELECT * FROM calls ORDER BY id").fetchone()
    assert json.loads(rejected["raw_output"]) == {"wrong": 1}
    assert "value" in rejected["error"]
    assert rejected["tokens_in"] == 100


def test_validation_does_not_depend_on_the_backend_option(dream, db):
    """A reply that is not JSON at all, as a harness without structured output
    returns it, is checked like any other output."""
    a = Scripted("a", ["Here is the JSON you asked for", {"value": 1}])
    assert dream(a, Scripted("b")) == "complete"
    assert calls(db) == [("a", "invalid"), ("a", "ok")]


def test_two_schema_failures_fail_the_input_and_keep_the_backend(dream, db):
    a = Scripted("a", [{"wrong": 1}, {"value": "seven"}])
    b = Scripted("b")
    assert dream(a, b, count=2) == "complete"
    assert step_states(db) == {"read:1": "failed", "read:2": "done"}
    assert "did not match" in step_reasons(db)["read:1"]
    # The input failed; the backend did not. The next input still uses it.
    assert calls(db) == [("a", "invalid"), ("a", "invalid"), ("a", "ok")]
    assert b.prompts == []


# Transport failures and fallback (5.3)


def test_two_transport_failures_move_the_call_to_the_next_backend(dream, db):
    a = Scripted("a", [BackendError("connection reset"), BackendError("timed out")])
    b = Scripted("b")
    assert dream(a, b, count=2) == "complete"
    assert step_states(db) == {"read:1": "done", "read:2": "done"}
    # `a` is unavailable for the rest of the run, so read:2 goes straight to b.
    assert calls(db) == [("a", "error"), ("a", "error"), ("b", "ok"), ("b", "ok")]
    errors = [row[0] for row in db.execute("SELECT error FROM calls WHERE backend='a'")]
    assert errors == ["connection reset", "timed out"]


def test_the_next_backend_gets_its_own_single_retry(dream, db):
    a = Scripted("a", [BackendError("down"), BackendError("down")])
    b = Scripted("b", [BackendError("blip"), {"value": 5}])
    assert dream(a, b) == "complete"
    assert results(db) == {"read:1": "[5]"}
    assert calls(db) == [("a", "error"), ("a", "error"), ("b", "error"), ("b", "ok")]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        # The last failure decides: a transport failure says nothing about the
        # input, so the call moves on; a schema failure fails the input.
        ([{"wrong": 1}, BackendError("down")], {"read:1": "done"}),
        ([BackendError("down"), {"wrong": 1}], {"read:1": "failed"}),
    ],
)
def test_mixed_failures_are_decided_by_the_last_one(dream, db, script, expected):
    assert dream(Scripted("a", script), Scripted("b")) == "complete"
    assert step_states(db) == expected


def test_no_backend_left_saves_progress_and_ends_partial(dream, db):
    a = Scripted("a", [{"value": 1}, BackendError("down"), BackendError("down")])
    b = Scripted("b", [BackendError("down"), BackendError("down")])
    status = dream(a, b, count=3)
    assert status == "partial"
    assert run.exit_code(status) == PARTIAL
    assert step_states(db) == {
        "read:1": "done",
        "read:2": "pending",
        "read:3": "pending",
    }
    assert results(db) == {"read:1": "[1]"}
    row = run_row(db)
    assert row["status"] == "partial"
    assert "no backend is left for the reader role" in row["error"]
    assert "a: down" in row["error"] and "b: down" in row["error"]


# Circuit breaker


def test_breaker_k_schema_failures_in_a_row_disable_the_backend(dream, db):
    a = Scripted("a", [{"wrong": 1}, {"wrong": 2}, {"wrong": 3}])
    b = Scripted("b", [{"value": 20}, {"value": 10}])
    assert dream(a, b, count=2, breaker_k=3) == "complete"
    # read:1 failed on a; read:2's failure was a's third in a row, so a is off
    # for the run, read:2 moved to b, and read:1 got another chance on b.
    assert step_states(db) == {"read:1": "done", "read:2": "done"}
    assert results(db) == {"read:2": "[20]", "read:1": "[10]"}
    assert len(a.prompts) == 3


def test_a_valid_output_resets_the_breaker(dream, db):
    script = [{"wrong": 1}, {"value": 1}, {"wrong": 2}, {"value": 2}]
    a, b = Scripted("a", script), Scripted("b")
    assert dream(a, b, count=2, breaker_k=2) == "complete"
    assert b.prompts == []


# Provider usage limits (5.3, Decision 16)


def test_a_usage_limit_waits_for_the_reset_and_repeats_the_call(dream, db):
    clock = FakeClock()  # 09:00 UTC
    seen = []
    clock.on_sleep = lambda seconds: seen.append(dict(run_row(db)))
    a = Scripted(
        "a",
        [BackendError(CLAUDE_LIMIT), BackendError("blip"), {"value": 3}],
        release_date="2026-05-01",
    )
    b = Scripted("b", context_tokens=32_768)
    assert dream(a, b, clock=clock) == "complete"
    # The wait was neither a retry nor a failure: after it, `a` still had its
    # retry for the transport failure, and b was never asked.
    assert results(db) == {"read:1": "[3]"}
    assert b.prompts == []
    # It slept until the reset plus a minute, and none of it was wall-clock time.
    assert clock.now() == datetime(2026, 11, 15, 13, 1, tzinfo=UTC)
    assert run_row(db)["active_seconds"] == pytest.approx(0)
    # While asleep the run was `waiting` with the reset time and the notice.
    assert seen[0]["status"] == "waiting"
    assert seen[0]["wait_until"].startswith("2026-11-15T13:01")
    notice = json.loads(seen[0]["wait_notice"])
    assert notice["backend"] == "a"
    assert notice["reset"].startswith("2026-11-15T13:00")
    assert notice["option"] == "fallback_on_usage_limit"
    assert "fallback_on_usage_limit" in notice["message"]
    assert "continue on b" in notice["message"]
    assert notice["current"] == {
        "backend": "a",
        "context_tokens": 200_000,
        "release_date": "2026-05-01",
    }
    assert notice["next"] == {
        "backend": "b",
        "context_tokens": 32_768,
        "release_date": None,
    }
    assert run_row(db)["wait_notice"] is None


def test_with_fallback_on_a_usage_limit_moves_on_at_once(dream, db):
    clock = FakeClock()
    a = Scripted("a", [BackendError(CLAUDE_LIMIT)])
    b = Scripted("b", [{"value": 9}])
    assert dream(a, b, count=2, clock=clock, fallback_on_usage_limit=True) == (
        "complete"
    )
    assert results(db) == {"read:1": "[9]", "read:2": "[2]"}
    assert len(a.prompts) == 1
    assert clock.seconds == 0


def test_with_fallback_on_but_no_other_backend_the_call_still_waits(db, home):
    cfg = two_backends(fallback_on_usage_limit=True)
    cfg["roles"]["reader"] = {"backends": ["a"]}
    run_id = add_run(db, cfg=cfg)
    clock = FakeClock()
    a = Scripted("a", [BackendError(CLAUDE_LIMIT)])
    runner = run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": Strict("reader")},
        backends={"a": a},
        planner=reads(1, review=False),
        clock=clock,
    )
    assert runner.execute() == "complete"
    assert clock.now().hour == 13


@pytest.mark.parametrize(
    "message",
    [
        CLAUDE_LIMIT,  # 4 hours away, beyond the 60-minute cap below
        "You've hit your usage limit.",  # no reset time
        "You've hit your limit · resets (UTC)",
        "You've hit your limit · resets 3pm (Mars/Olympus_Mons)",
        "Quota exceeded; please slow down",  # an unrecognised form
    ],
)
def test_unusable_usage_limits_are_transport_failures(dream, db, message):
    clock = FakeClock()
    a = Scripted("a", [BackendError(message), BackendError(message)])
    b = Scripted("b")
    assert dream(a, b, clock=clock, max_usage_wait_minutes=60) == "complete"
    assert calls(db) == [("a", "error"), ("a", "error"), ("b", "ok")]
    assert clock.seconds == 0


def test_an_interrupt_while_waiting_stops_the_run(db, home):
    cfg = two_backends()
    run_id = add_run(db, cfg=cfg)
    clock = FakeClock()
    runner = run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": Strict("reader")},
        backends={"a": Scripted("a", [BackendError(CLAUDE_LIMIT)]), "b": Scripted("b")},
        planner=reads(1, review=False),
        clock=clock,
    )
    clock.on_sleep = lambda seconds: runner.stop()
    assert runner.execute() == "interrupted"
    assert clock.seconds < 3600


# Usage-limit messages: one fixture per known form

NOW = datetime(2026, 11, 15, 9, 30, tzinfo=UTC)
DUBLIN = ZoneInfo("Europe/Dublin")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Claude, clock only, with and without a zone and minutes.
        (
            "You've hit your limit · resets 3am (Australia/Melbourne)",
            datetime(2026, 11, 16, 3, 0, tzinfo=ZoneInfo("Australia/Melbourne")),
        ),
        (
            "You've hit your session limit · resets 4:30pm (Europe/Dublin)",
            datetime(2026, 11, 15, 16, 30, tzinfo=DUBLIN),
        ),
        ("You've hit your limit · resets 3am", datetime(2026, 11, 16, 3, tzinfo=UTC)),
        ("5-hour limit reached ∙ resets 11pm", datetime(2026, 11, 15, 23, tzinfo=UTC)),
        # Claude, dated.
        (
            "You've hit your weekly limit · resets Nov 20, 5pm (Europe/Dublin)",
            datetime(2026, 11, 20, 17, tzinfo=DUBLIN),
        ),
        (
            "You've hit your Opus limit · resets Jan 2 at 10am",
            datetime(2027, 1, 2, 10, tzinfo=UTC),
        ),
        (
            "Claude AI usage limit reached|1763226000",
            datetime.fromtimestamp(1763226000, UTC),
        ),
        # Codex, clock only and dated.
        (
            "ERROR: You've hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit "
            "https://chatgpt.com/codex/settings/usage to purchase more credits or "
            "try again at 4:18 PM.",
            datetime(2026, 11, 15, 16, 18, tzinfo=UTC),
        ),
        (
            "You've hit your usage limit. Upgrade to Pro or try again at 12 PM.",
            datetime(2026, 11, 15, 12, tzinfo=UTC),
        ),
        (
            "You've hit your usage limit. ... or try again at Sep 5th, 2027 1:39 AM.",
            datetime(2027, 9, 5, 1, 39, tzinfo=UTC),
        ),
        (
            "You've hit your usage limit. ... or try again at September 6, 2027 "
            "1:39 AM.",
            datetime(2027, 9, 6, 1, 39, tzinfo=UTC),
        ),
    ],
)
def test_usage_limit_reset_times(message, expected):
    assert usage_limit_reset(message, NOW) == expected


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your usage limit.",
        "You've hit your limit · resets (UTC)",
        "You've hit your limit · resets 13pm",
        "You've hit your limit · resets Smarch 4, 3pm",
        "You've hit your limit · resets 3pm (Not/AZone)",
        "rate limit exceeded, retry in 30 seconds",
        "connection reset by peer at 3pm",
        "",
    ],
)
def test_unrecognised_usage_limit_messages_have_no_reset(message):
    assert usage_limit_reset(message, NOW) is None


def test_a_clock_time_without_a_zone_is_read_in_the_zone_of_now():
    now = NOW.astimezone(DUBLIN)
    assert usage_limit_reset("hit your limit · resets 3pm", now) == datetime(
        2026, 11, 15, 15, tzinfo=DUBLIN
    )


# Accounting (1.2, 5.6) and sizing (5.2)


def test_missing_usage_is_estimated_from_the_prompt_and_output_and_flagged(dream, db):
    a = Scripted("a", [CallResult({"value": 4}, None, None, session_ref="s")])
    assert dream(a, Scripted("b")) == "complete"
    row = db.execute("SELECT * FROM calls").fetchone()
    assert row["usage_estimated"] == 1
    assert row["tokens_in"] == estimate_tokens(a.prompts[0])
    assert row["tokens_out"] == estimate_tokens(json.dumps({"value": 4}))


def test_every_attempt_is_recorded_with_its_backend_and_counted(dream, db):
    a = Scripted("a", [{"wrong": 1}, BackendError("down")])
    b = Scripted("b", [CallResult({"value": 1}, 7, 3, model="b-model")])
    assert dream(a, b) == "complete"
    rows = db.execute("SELECT backend, model, tokens_in, tokens_out FROM calls")
    assert [tuple(row) for row in rows] == [
        ("a", None, 100, 20),
        ("a", None, 0, 0),
        ("b", "b-model", 7, 3),
    ]


def test_inputs_are_sized_for_the_smallest_context_in_the_fallback_list():
    cfg = two_backends()
    assert config.role_context_tokens(cfg, "reader") == 32_768
    assert roles.input_tokens(cfg, "reader", "x" * 4_000) == 32_768 - 8_192 - 1_000
    # A resume that changes the role's backends re-sizes what is left (5.4).
    resumed = config.apply_overrides(cfg, {"roles": {"reader": ["a"]}})
    assert config.role_context_tokens(resumed, "reader") == 200_000


# Timeouts


def test_a_timed_out_call_is_killed_with_its_process_group(tmp_path):
    pidfile = tmp_path / "grandchild"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    result = run_process(["sh", "-c", script], timeout=1)
    assert result.timed_out
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the call's child process survived the timeout")


def test_waiting_does_not_count_toward_the_wall_clock_limit(db, home):
    cfg = two_backends()
    cfg["budget"]["wall_clock_minutes"] = 1
    run_id = add_run(db, cfg=cfg)
    clock = FakeClock()
    a = Scripted("a", [BackendError(CLAUDE_LIMIT)])
    runner = run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": Strict("reader")},
        backends={"a": a, "b": Scripted("b")},
        planner=reads(2, review=False),
        clock=clock,
    )
    assert runner.execute() == "complete"
    assert clock.now() - datetime(2026, 11, 15, 9, tzinfo=UTC) > timedelta(hours=3)
    assert step_states(db, run_id) == {"read:1": "done", "read:2": "done"}

"""Sequential, recorded role calls with one retry per backend and bounded waits."""

import calendar
import json
import math
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import ValidationError, validate

from dreaming.backends.probe import ProbeResult
from dreaming.budget import RunBudget
from dreaming.config import Config
from dreaming.sessions import SessionError
from dreaming.steps import CallResult, Steps
from dreaming.store import Store


class Backend(Protocol):
    name: str
    context_tokens: int
    supports_tools: bool

    def probe(self) -> ProbeResult: ...

    def call(self, role: str, prompt: str, schema: dict, tools: list[str]) -> CallResult: ...


def estimated_tokens(value: str) -> int:
    return math.ceil(len(value.encode("utf-8")) / 4)


def input_tokens(prompt: str, schema: dict, tools: list[str]) -> int:
    return estimated_tokens(json.dumps({"prompt": prompt, "schema": schema, "tools": tools}))


def usage_reset(text: str, now: datetime) -> datetime | None:
    """Recognise provider limit messages; the caller supplies the local, aware clock."""
    if not re.search(r"(?:usage|session) limit|hit your limit", text, re.I):
        return None
    match = re.search(
        r"(?:resets?(?: at)?|try again at)\s+"
        r"(?:([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+)?"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b(?:\s*\(([^)]+)\))?",
        text,
        re.I,
    )
    if not match or now.tzinfo is None:
        return None
    month, day, year, hour, minute, ampm, zone = match.groups()
    try:
        tz = ZoneInfo(zone) if zone else now.tzinfo
        local = now.astimezone(tz)
        h, m = int(hour), int(minute or 0)
        if not 1 <= h <= 12 or not 0 <= m < 60:
            return None
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
        if month:
            months = {name[:3].lower(): n for n, name in enumerate(calendar.month_name) if n}
            result = datetime(int(year), months[month[:3].lower()], int(day), h, m, tzinfo=tz)
            return result if result > local else None
        result = local.replace(hour=h, minute=m, second=0, microsecond=0)
        return result + timedelta(days=1) if result <= local else result
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        return None


class Dispatcher:
    def __init__(
        self,
        store: Store,
        run_id: str,
        config: Config,
        backends: dict[str, Backend],
        *,
        budget: RunBudget | None = None,
        resumed: bool = False,
        clock=time.monotonic,
        wall_clock=lambda: datetime.now().astimezone(),
        sleep=time.sleep,
        interrupted=lambda: False,
    ):
        self.store, self.run_id, self.config, self.backends = store, run_id, config, backends
        self.steps = Steps(store, run_id)
        self.budget = budget or RunBudget(store, run_id, config.budget, clock=clock)
        self.clock, self.wall_clock, self.sleep, self.interrupted = (
            clock,
            wall_clock,
            sleep,
            interrupted,
        )
        stats = json.loads(
            store.connection.execute(
                "SELECT statistics FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        self.unavailable = set() if resumed else set(stats.get("backend_unavailable", []))
        self.streaks = {} if resumed else stats.get("schema_streaks", {})
        self.checked: set[str] = set()
        self._save_state()

    def _save_state(self):
        self.store.connection.execute(
            "UPDATE runs SET statistics=json_set(statistics, '$.backend_unavailable', json(?), "
            "'$.schema_streaks', json(?)) WHERE id=?",
            (json.dumps(sorted(self.unavailable)), json.dumps(self.streaks), self.run_id),
        )

    def context_limit(self, role: str) -> int:
        return min(
            min(
                self.config.backends[name].context_tokens,
                getattr(
                    self.backends.get(name),
                    "context_tokens",
                    self.config.backends[name].context_tokens,
                ),
            )
            for name in self.config.roles[role]
        )

    def _wait(self, until: datetime, notice: str) -> bool:
        self.budget.pause(until.isoformat(), notice)
        while (remaining := (until - self.wall_clock()).total_seconds()) > 0:
            if self.interrupted():
                return False
            self.sleep(min(1, remaining))
        if self.interrupted():
            return False
        self.budget.resume()
        return True

    def _notice(self, name: str, next_name: str | None, reset: datetime) -> str:
        def describe(backend_name):
            if backend_name is None:
                return "none"
            backend = self.backends.get(backend_name)
            context = self.config.backends[backend_name].context_tokens
            release = getattr(backend, "release_date", None)
            return (
                f"{backend_name} ({context} context tokens"
                + (f", released {release}" if release else "")
                + ")"
            )

        return (
            f"{describe(name)} usage limit until {reset.isoformat()}. "
            f"Set fallback_on_usage_limit=true to continue on {describe(next_name)}."
        )

    def dispatch(
        self,
        key: str,
        role: str,
        prompt: str,
        schema: dict,
        tools: list[str],
        *,
        sessions: tuple[tuple[str, str], ...] = (),
        input_ref: str | None = None,
        checkpoint=None,
    ) -> CallResult:
        # An answer saved before a crash is reapplied, never regenerated.
        saved = self.steps.saved_output(key)
        if saved is not None:
            return CallResult(saved)
        context = self.context_limit(role)
        if input_tokens(prompt, schema, tools) > context - min(4096, context // 4):
            raise ValueError(
                "Role input exceeds the smallest fallback context; resize pending input"
            )
        if self.store.connection.execute(
            "SELECT 1 FROM calls WHERE run_id=? AND outcome IS NULL", (self.run_id,)
        ).fetchone():
            return CallResult(
                "", outcome="partial", error="indeterminate call; no durable response"
            )
        waiting = self.store.connection.execute(
            "SELECT wait_until,notice FROM runs WHERE id=?", (self.run_id,)
        ).fetchone()
        if waiting[0] and not self._wait(
            datetime.fromisoformat(waiting[0]), waiting[1] or "Usage limit"
        ):
            return CallResult("", outcome="interrupted")
        choices = self.config.roles[role]
        for index, name in enumerate(choices):
            backend = self.backends.get(name)
            if name in self.unavailable or backend is None:
                continue
            if tools and not backend.supports_tools:
                self.unavailable.add(name)
                self._save_state()
                continue
            if not self.budget.can_call(role, key=key, sessions=sessions):
                return CallResult("", outcome="budget_exhausted")
            if name not in self.checked and hasattr(backend, "probe"):
                self.checked.add(name)
                if hasattr(backend, "budget"):
                    backend.budget = self.budget
                if not backend.probe().compatible:
                    self.unavailable.add(name)
                    self._save_state()
                    continue
            failures = 0
            while failures < 2:
                if self.interrupted():
                    return CallResult("", outcome="interrupted")
                if not self.budget.can_call(role, key=key, sessions=sessions):
                    return CallResult("", outcome="budget_exhausted")
                session_ref = getattr(backend, "new_session", lambda: None)()
                ref = self.steps.begin_call(
                    key,
                    role=role,
                    backend=name,
                    model=self.config.backends[name].model,
                    session_ref=session_ref,
                    sessions=sessions,
                    input_ref=input_ref,
                )
                start = self.clock()
                try:
                    response = backend.call(role, prompt, schema, tools)
                except (OSError, ValueError) as exc:
                    response = CallResult("", outcome="transport_failure", error=str(exc))
                response = replace(response, seconds=max(response.seconds, self.clock() - start))
                if (
                    response.tokens_in == response.tokens_out == 0
                    and response.outcome != "context_changed"
                ):
                    response = replace(
                        response,
                        tokens_in=input_tokens(prompt, schema, tools),
                        tokens_out=estimated_tokens(response.raw_output),
                        usage_estimated=True,
                    )
                if response.outcome == "success":
                    try:
                        validate(json.loads(response.raw_output), schema)
                    except (ValueError, ValidationError) as exc:
                        response = replace(response, outcome="schema_failure", error=str(exc))
                if response.outcome == "success" and checkpoint is not None:
                    try:
                        response = replace(response, raw_output=checkpoint(response, name))
                    except SessionError as exc:
                        if exc.code not in {"forgotten", "forgotten_input"}:
                            raise
                        response = replace(
                            response, raw_output="", outcome="input_rejected", error=exc.code
                        )
                reset = (
                    usage_reset(response.error or response.raw_output, self.wall_clock())
                    if response.outcome != "success"
                    else None
                )
                if reset:
                    response = replace(response, outcome="usage_limit")
                self.steps.record_call(ref, response)
                self.budget.checkpoint()
                if response.outcome == "input_rejected":
                    return response
                if response.outcome == "context_changed":
                    return response
                if response.outcome == "success":
                    self.streaks[name] = 0
                    self._save_state()
                    return response
                if reset and self.config.thresholds.fallback_on_usage_limit:
                    self.unavailable.add(name)
                    self._save_state()
                    break
                if (
                    reset
                    and (reset - self.wall_clock()).total_seconds()
                    <= self.config.thresholds.max_usage_wait
                ):
                    until = reset + timedelta(seconds=1)
                    next_name = next(
                        (n for n in choices[index + 1 :] if n not in self.unavailable), None
                    )
                    if not self._wait(until, self._notice(name, next_name, reset)):
                        return replace(response, outcome="interrupted")
                    continue
                failures += 1
                self.streaks[name] = (
                    self.streaks.get(name, 0) + 1 if response.outcome == "schema_failure" else 0
                )
                if self.streaks[name] >= self.config.thresholds.breaker_k:
                    self.unavailable.add(name)
                    # Earlier inputs failed on this backend can now use a fallback.
                    self.store.connection.execute(
                        "UPDATE steps SET state='pending',reason=NULL WHERE run_id=? "
                        "AND (state='failed' OR (state='done' AND result=?)) "
                        "AND EXISTS (SELECT 1 FROM calls c "
                        "WHERE c.run_id=steps.run_id AND c.task_key=steps.key "
                        "AND c.backend=? AND c.outcome='schema_failure')",
                        (self.run_id, '{"omitted":"schema_failure"}', name),
                    )
                    self._save_state()
                    break
                if failures == 2 and response.outcome == "schema_failure":
                    with self.store.transaction() as conn:
                        conn.execute(
                            "UPDATE steps SET state='failed',reason=? WHERE run_id=? AND key=?",
                            ("role schema failed twice", self.run_id, key),
                        )
                        for source, sid in sessions:
                            conn.execute(
                                "INSERT INTO sessions_seen(source,session_id,state,reason) "
                                "VALUES (?,?,'failed',?) ON CONFLICT(source,session_id) "
                                "DO UPDATE SET state='failed',reason=excluded.reason",
                                (source, sid, "role schema failed twice"),
                            )
                    self._save_state()
                    return response
            self.unavailable.add(name)
            self._save_state()
        return CallResult("", outcome="partial", error=f"No compatible backend left for {role}")

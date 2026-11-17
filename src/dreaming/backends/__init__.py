"""Model backends: harness adapters, the direct LM Studio adapter, probes and the
dispatcher that applies one retry and fallback policy to every call."""

import json
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dreaming.errors import Partial

if TYPE_CHECKING:
    from dreaming.backends.probe import ProbeResult


@dataclass
class CallResult:
    """One answered model call (design, Roles and calls).

    `output` is the parsed JSON reply, or the raw text when the reply was not
    JSON; the caller validates it. Tokens are None when the backend did not
    report usage; the caller then estimates them.
    """

    output: Any
    tokens_in: int | None
    tokens_out: int | None
    usage_estimated: bool = False
    session_ref: str = ""  # harness session id, or LM Studio thread id == run id
    model: str | None = None


class Backend(Protocol):
    name: str
    context_tokens: int
    release_date: str
    # True when the harness takes a session id chosen before the call (Claude
    # `--session-id`), so the calls row can record it before the call starts.
    accepts_session_id: bool

    def probe(self) -> ProbeResult: ...

    def call(
        self,
        role: str,
        prompt: str,
        schema: dict,
        tools: list[str],
        session_id: str | None = None,
        workdir: Path | None = None,
    ) -> CallResult:
        """`workdir` is the only directory the call's file tools may write: the
        author's work copy (9.1, 9.4). Without one, a call that has writing
        tools gets no directory outside its own working directory."""


class BackendError(Exception):
    """A call that failed: the harness, provider or transport reported an error.
    The message is the raw error, recorded on the calls row (5.3)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SchemaError(Exception):
    """A reply that does not match the role's schema (4.7)."""


class InputFailed(Exception):
    """The input got no valid output in two attempts; it is recorded as failed
    and not retried by later dreams unless selected again (4.7)."""

    def __init__(self, message: str, backend: str) -> None:
        super().__init__(message)
        self.message = message
        self.backend = backend


def parse_reply(text: str | None) -> Any:
    """A reply's JSON value, or the text itself when it is not JSON; the caller
    checks it against the role's schema either way."""
    try:
        return json.loads(text or "")
    except json.JSONDecodeError:
        return text


def call_dir(home: Path, run_id: str) -> Path:
    """The working directory of a run's model calls. It is under the home, so no
    instruction file above it loads and its sessions count as the dream's own
    (1.5)."""
    path = home / "runs" / run_id / "calls"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_tokens(*texts: str) -> int:
    """`ceil(utf8 bytes / 4)`, the estimate used when a harness omits usage and
    for sizing inputs."""
    return -(-sum(len(text.encode()) for text in texts) // 4)


# Tools that change files; a call gets them only for its work copy (9.4).
WRITE_TOOLS = ("edit", "write")


# Processes


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stopped: bool = False  # killed because `stop_when` matched an output line


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    stdin_text: str | None = None,
    stop_when: Callable[[str], bool] | None = None,
) -> ProcessResult:
    """Run a command in its own process group and kill the group on timeout, or
    as soon as an output line satisfies `stop_when`.

    Descriptors beyond stdio are closed, so a model call never inherits the
    dream lock (design, Lock).
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return ProcessResult(127, "", f"{argv[0]}: {exc.strerror or exc}")
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill_group(proc)

    def feed() -> None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except BrokenPipeError, ValueError:
            pass

    stderr: list[str] = []
    threads = [threading.Thread(target=lambda: stderr.extend(proc.stderr))]
    if stdin_text is not None:
        threads.append(threading.Thread(target=feed))
    timer = threading.Timer(timeout, expire)
    timer.start()
    for thread in threads:
        thread.start()
    lines, stopped = [], False
    try:
        for line in proc.stdout:
            lines.append(line)
            if stop_when is not None and not stopped and stop_when(line):
                stopped = True
                _kill_group(proc)
        proc.wait()
    finally:
        timer.cancel()
        for thread in threads:
            thread.join()
    return ProcessResult(
        proc.returncode,
        "".join(lines),
        "".join(stderr),
        timed_out=timed_out.is_set(),
        stopped=stopped,
    )


# Provider usage limits (5.3, Decision 16)

USAGE_LIMIT_MARGIN = timedelta(minutes=1)

# Claude: "resets 3am (Australia/Melbourne)", "resets 4:30pm", "resets Nov 20,
# 5pm (Europe/Dublin)", "resets Jan 2 at 10am". Codex: "try again at 4:18 PM.",
# "try again at Sep 5th, 2026 1:39 AM.".
_RESET = re.compile(
    r"(?:\bresets?|\btry again at)\s+"
    r"(?:(?P<month>[a-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4})\s+)?(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b"
    r"(?:\s*\((?P<zone>[^)]+)\))?",
    re.IGNORECASE,
)
# Older Claude Code: "Claude AI usage limit reached|<unix seconds>".
_EPOCH = re.compile(r"usage limit reached\|(?P<epoch>\d{9,11})", re.IGNORECASE)


def usage_limit_reset(text: str, now: datetime) -> datetime | None:
    """The reset time of a provider usage-limit message, or None when the text is
    not a usage limit or its reset time cannot be read.

    A function of the text and the current time only. A time without a date is
    the next occurrence of that clock time; one without a zone is read in the
    zone of `now`, which the caller sets to the local zone the harness prints.
    """
    if "limit" not in text.lower():
        return None
    epoch = _EPOCH.search(text)
    if epoch:
        return datetime.fromtimestamp(int(epoch["epoch"]), UTC)
    match = _RESET.search(text)
    if match is None:
        return None
    hour = int(match["hour"])
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    try:
        zone = ZoneInfo(match["zone"].strip()) if match["zone"] else now.tzinfo
        local = now.astimezone(zone)
        reset = local.replace(
            hour=hour, minute=int(match["minute"] or 0), second=0, microsecond=0
        )
        if match["month"]:
            month = datetime.strptime(match["month"][:3], "%b").month
            year = int(match["year"] or local.year)
            reset = reset.replace(year=year, month=month, day=int(match["day"]))
            if not match["year"] and reset < local:
                reset = reset.replace(year=year + 1)
        elif reset < local:
            reset += timedelta(days=1)
    except ValueError, ZoneInfoNotFoundError:
        return None
    return reset


# The dispatcher


def _facts(backend: Backend | None) -> dict | None:
    if backend is None:
        return None
    return {
        "backend": backend.name,
        "context_tokens": backend.context_tokens,
        "release_date": backend.release_date or None,
    }


class Dispatcher:
    """One retry policy for every call (design, Roles and calls).

    A call gets two attempts on a backend. After two failures the last one
    decides: a transport failure marks the backend unavailable for the run and
    the call moves to the role's next backend, which gets its own two attempts;
    a schema failure fails the input. `breaker_k` schema failures in a row on
    one backend also mark it unavailable; the inputs that failed on it are
    listed in `tripped` so the runner can give them to the next backend. A
    usage limit with a reset time within `max_usage_wait` is waited out and
    the call repeated, which counts as neither attempt nor failure.

    The state lives for one process, so a resumed run starts with every
    backend eligible again (5.4).
    """

    def __init__(self, thresholds: dict) -> None:
        self.breaker_k = thresholds["breaker_k"]
        self.max_wait = timedelta(minutes=thresholds["max_usage_wait_minutes"])
        self.fallback_on_usage_limit = thresholds["fallback_on_usage_limit"]
        self.unavailable: dict[str, str] = {}
        self.tripped: list[str] = []
        self._schema_streak: dict[str, int] = {}

    def call(
        self,
        role: str,
        backends: Sequence[Backend],
        attempt: Callable[[Backend], CallResult],
        wait: Callable[[datetime, dict], None],
        now: Callable[[], datetime],
    ) -> CallResult:
        """The result of the first backend to answer; `attempt` makes and records
        one call, and `wait` sleeps until a usage limit resets."""
        usable = [b for b in backends if b.name not in self.unavailable]
        for index, backend in enumerate(usable):
            result = self._on(backend, usable[index + 1 :], role, attempt, wait, now)
            if result is not None:
                return result
        reasons = "; ".join(f"{b.name}: {self.unavailable[b.name]}" for b in backends)
        detail = f" ({reasons})" if reasons else ""
        raise Partial(f"no backend is left for the {role} role{detail}")

    def _on(
        self,
        backend: Backend,
        following: Sequence[Backend],
        role: str,
        attempt: Callable[[Backend], CallResult],
        wait: Callable[[datetime, dict], None],
        now: Callable[[], datetime],
    ) -> CallResult | None:
        """The call on one backend, or None once that backend is unavailable."""
        failures: list[tuple[str, str]] = []
        while len(failures) < 2:
            try:
                result = attempt(backend)
            except SchemaError as exc:
                streak = self._schema_streak.get(backend.name, 0) + 1
                self._schema_streak[backend.name] = streak
                if streak >= self.breaker_k:
                    reason = f"{streak} outputs in a row missed the schema"
                    self.unavailable[backend.name] = reason
                    self.tripped.append(backend.name)
                    return None
                failures.append(("schema", str(exc)))
            except BackendError as exc:
                current = now().astimezone()
                reset = usage_limit_reset(exc.message, current)
                if reset is None or reset - current > self.max_wait:
                    failures.append(("transport", exc.message))
                elif self.fallback_on_usage_limit and following:
                    self.unavailable[backend.name] = exc.message
                    return None
                else:
                    until = max(reset, current) + USAGE_LIMIT_MARGIN
                    wait(until.astimezone(UTC), self._notice(backend, following, reset))
            else:
                self._schema_streak[backend.name] = 0
                return result
        kind, message = failures[-1]
        if kind == "schema":
            raise InputFailed(
                f"the {role} output did not match its schema twice on "
                f"{backend.name}: {message}",
                backend.name,
            )
        self.unavailable[backend.name] = message
        return None

    def _notice(
        self, backend: Backend, following: Sequence[Backend], reset: datetime
    ) -> dict:
        """What `dream status` and the run log show while the run waits (5.3)."""
        nxt = following[0] if following else None
        if nxt is None:
            message = (
                f"waiting for {backend.name}'s usage limit to reset; no other "
                "backend is configured for this role"
            )
        else:
            message = (
                f"waiting for {backend.name}'s usage limit to reset; set "
                "fallback_on_usage_limit = true (or start with "
                f"--fallback-on-usage-limit) to continue on {nxt.name} instead. "
                "Compare their context sizes: a smaller context may not fit "
                "larger tasks"
            )
        return {
            "backend": backend.name,
            "reset": reset.isoformat(timespec="minutes"),
            "option": "fallback_on_usage_limit",
            "message": message,
            "current": _facts(backend),
            "next": _facts(nxt),
        }

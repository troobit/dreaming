"""Token and wall-clock budgets of a run (1.2, 5.6, Decision 16).

Tokens used are always summed from the `calls` rows, so they include every
retry and fallback. Wall-clock time counts only while the dream runs: the run
row keeps the seconds spent, and the clock pauses while the run waits for a
provider usage limit to reset.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime


class SystemClock:
    """The real time source; tests pass a fake with the same three methods."""

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    def now(self) -> datetime:
        return datetime.now(UTC)


class WallClock:
    """Seconds the run has spent running, carried over from earlier parts."""

    def __init__(self, spent: float, monotonic: Callable[[], float]) -> None:
        self._spent = spent
        self._monotonic = monotonic
        self._since: float | None = monotonic()

    def pause(self) -> None:
        if self._since is not None:
            self._spent += self._monotonic() - self._since
            self._since = None

    def resume(self) -> None:
        if self._since is None:
            self._since = self._monotonic()

    @property
    def spent(self) -> float:
        running = 0.0 if self._since is None else self._monotonic() - self._since
        return self._spent + running


class LimitReached(Exception):
    """A limit stops new model calls. `exhausted` is true for the run's own total
    limits, which end it as `budget_exhausted`; false for the Read share, which
    only ends reading."""

    def __init__(self, reason: str, exhausted: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exhausted = exhausted


def tokens_used(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM calls WHERE run = ?",
        (run_id,),
    ).fetchone()
    return row[0]


def usage_by_backend(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Calls, tokens and seconds per backend and model (5.6)."""
    rows = conn.execute(
        "SELECT backend, model, COUNT(*) AS calls, SUM(tokens_in) AS tokens_in,"
        " SUM(tokens_out) AS tokens_out, COALESCE(SUM(seconds), 0) AS seconds"
        " FROM calls WHERE run = ? GROUP BY backend, model ORDER BY backend, model",
        (run_id,),
    )
    return [dict(row) for row in rows]


class Budget:
    """The limits of one run, checked before every step and every call.

    Read calls may only use the share of the token budget not reserved for
    review and authoring.
    """

    def __init__(
        self, conn: sqlite3.Connection, run_id: str, limits: dict, clock: WallClock
    ) -> None:
        self.conn = conn
        self.run_id = run_id
        self.tokens = limits["tokens"]
        self.read_tokens = limits["tokens"] * (1 - limits["review_author_reserve"])
        self.seconds = limits["wall_clock_minutes"] * 60
        self.clock = clock

    def check(self, phase: str) -> None:
        used = tokens_used(self.conn, self.run_id)
        if used >= self.tokens:
            raise LimitReached(f"token budget reached ({used} of {self.tokens})", True)
        if self.clock.spent >= self.seconds:
            raise LimitReached(
                f"wall-clock limit reached ({self.clock.spent / 60:.1f} of "
                f"{self.seconds / 60:.0f} minutes)",
                True,
            )
        if phase == "read" and used >= self.read_tokens:
            raise LimitReached(
                f"Read share of the token budget used ({used} of "
                f"{self.read_tokens:.0f}); the rest is reserved for review and "
                "authoring",
                False,
            )

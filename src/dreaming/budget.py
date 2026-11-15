"""Accounting from recorded calls and checkpoints of active process time."""

import json
import time
from dataclasses import asdict

from dreaming.config import Budget
from dreaming.store import Store


class RunBudget:
    def __init__(self, store: Store, run_id: str, limits: Budget, *, clock=time.monotonic):
        self.store, self.run_id, self.limits, self.clock = store, run_id, limits, clock
        row = store.connection.execute(
            "SELECT statistics,status FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        self.statistics = json.loads(row[0])
        self.elapsed = self.statistics.get("active_seconds", 0.0)
        self.since = clock() if row[1] != "waiting" else None

    @property
    def tokens_used(self) -> int:
        return self.store.connection.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM calls WHERE run_id=?", (self.run_id,)
        ).fetchone()[0]

    @property
    def seconds_used(self) -> float:
        return self.elapsed + (max(0, self.clock() - self.since) if self.since is not None else 0)

    def checkpoint(self) -> None:
        self.elapsed = self.seconds_used
        if self.since is not None:
            self.since = self.clock()
        # Preserve statistics written by feedback or other concurrent operations.
        self.store.connection.execute(
            "UPDATE runs SET statistics=json_set(statistics,'$.active_seconds',?) WHERE id=?",
            (self.elapsed, self.run_id),
        )

    def pause(self, until: str, notice: str) -> None:
        self.checkpoint()
        self.since = None
        self.store.connection.execute(
            "UPDATE runs SET status='waiting',wait_until=?,notice=? WHERE id=?",
            (until, notice, self.run_id),
        )

    def resume(self) -> None:
        if self.since is None:
            self.since = self.clock()
        self.store.connection.execute(
            "UPDATE runs SET status='running',wait_until=NULL,notice=NULL WHERE id=?",
            (self.run_id,),
        )

    def exhausted(self) -> bool:
        return (
            self.tokens_used >= self.limits.max_tokens
            or self.seconds_used >= self.limits.max_seconds
        )

    @property
    def read_sessions(self) -> set[str]:
        rows = self.store.connection.execute(
            "SELECT c.task_key,i.source,i.session_id,s.thread_id FROM calls c "
            "JOIN steps t ON t.run_id=c.run_id AND t.key=c.task_key "
            "LEFT JOIN session_inputs i ON i.call_id=c.id "
            "LEFT JOIN sessions_seen s ON s.source=i.source AND s.session_id=i.session_id "
            "WHERE c.run_id=? AND c.role='reader' AND c.outcome='success' "
            "AND t.state='done' AND t.phase='Read'",
            (self.run_id,),
        )
        return {
            f"thread:{thread}" if thread else f"session:{source}:{sid}" if sid else f"step:{key}"
            for key, source, sid, thread in rows
        }

    def read_limit_reached(self, key: str, sessions: tuple[tuple[str, str], ...]) -> bool:
        requested = set()
        for source, sid in sessions:
            row = self.store.connection.execute(
                "SELECT thread_id FROM sessions_seen WHERE source=? AND session_id=?", (source, sid)
            ).fetchone()
            requested.add(f"thread:{row[0]}" if row and row[0] else f"session:{source}:{sid}")
        requested = requested or {f"step:{key}"}
        return len(self.read_sessions | requested) > self.limits.max_sessions

    def can_call(
        self, role: str, *, key: str | None = None, sessions: tuple[tuple[str, str], ...] = ()
    ) -> bool:
        ceiling = self.limits.max_tokens
        phase = (
            self.store.connection.execute(
                "SELECT phase FROM steps WHERE run_id=? AND key=?", (self.run_id, key)
            ).fetchone()
            if key is not None
            else None
        )
        if role == "reader" and (phase is None or phase[0] == "Read"):
            ceiling *= 1 - self.limits.reserve_share
            if key is not None and self.read_limit_reached(key, sessions):
                return False
        return not self.exhausted() and self.tokens_used < ceiling

    def summary(self) -> dict:
        return {
            **asdict(self.limits),
            "tokens_used": self.tokens_used,
            "seconds_used": self.seconds_used,
            "sessions_used": len(self.read_sessions),
        }

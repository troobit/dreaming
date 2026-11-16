"""Before/after rates for applied changes, re-candidacy and calibration statistics.

Numbers are raw counts labelled association: a change is never shown as having
caused a drop. Forgotten sessions and unavailable observations are excluded by
reusing the shared evidence views.
"""

import json
from datetime import UTC, datetime

from dreaming.knowledge import _rows, available_observations, interaction_key
from dreaming.report import _escape, _table
from dreaming.sessions import _timestamp

LABEL = "association"


def _in_scope(session: dict, scope: dict) -> bool:
    return all(
        not scope.get(dimension) or session[field] in scope[dimension]
        for dimension, field in (
            ("domains", "domain"),
            ("projects", "project"),
            ("workflows", "workflow"),
        )
    )


def _counts(observations, sessions, applied):
    result = {}
    for side, keep in (
        ("before", lambda t: t < applied),
        ("after", lambda t: t >= applied),
    ):
        result[side] = {
            "observations": sum(1 for _, t in observations if keep(t)),
            "sessions": len({key for key, t in sessions if keep(t)}),
        }
    return result


def effect_measurement(store, config, change_id, *, now=None) -> dict:
    """Raw counts around `applied_at`; `insufficient_evidence` carries its reason."""
    rows = _rows(store.connection.execute("SELECT * FROM changes WHERE id=?", (change_id,)))
    if not rows:
        raise KeyError(change_id)
    change = rows[0]
    base = {"change_id": change_id, "pattern_id": change["pattern_id"], "label": LABEL}
    applied = _timestamp(change["applied_at"])
    if applied is None or not change["pattern_id"]:
        return base | {"status": "not_applied"}
    base |= {"applied_at": change["applied_at"], "reverted_at": change["reverted_at"]}
    pattern = store.connection.execute(
        "SELECT lens_scope FROM patterns WHERE id=?", (change["pattern_id"],)
    ).fetchone()
    scope = json.loads(pattern[0]) if pattern else {}
    observations = [
        (interaction_key(r), _timestamp(r["observed_at"]))
        for r in available_observations(store, pattern_id=change["pattern_id"])
        if not r["imported"] and _timestamp(r["observed_at"])
    ]
    sessions = []
    for row in _rows(
        store.connection.execute(
            "SELECT * FROM sessions_seen WHERE state='read' AND forgotten=0 "
            "AND started_at IS NOT NULL"
        )
    ):
        started = _timestamp(row["started_at"])
        if started and _in_scope(row, scope):
            sessions.append((interaction_key(row), started))
    counts = _counts(
        [(k, t) for k, t in observations],
        sessions,
        applied,
    )
    result = base | counts
    evidence = json.loads(change["evidence"])
    if not observations or (
        evidence
        and not store.connection.execute(
            f"SELECT 1 FROM observations WHERE id IN ({','.join('?' * len(evidence))}) LIMIT 1",
            evidence,
        ).fetchone()
    ):
        reason = "file_only"
    elif not counts["before"]["sessions"]:
        reason = "no_baseline_sessions"
    elif min(counts["before"]["sessions"], counts["after"]["sessions"]) < (
        config.thresholds.min_sessions_for_rate
    ):
        reason = "too_few_sessions"
    elif not counts["before"]["observations"]:
        reason = "no_baseline_observations"
    else:
        return result | {
            "status": "measured",
            "before_rate": counts["before"]["observations"] / counts["before"]["sessions"],
            "after_rate": counts["after"]["observations"] / counts["after"]["sessions"],
        }
    return result | {"status": "insufficient_evidence", "reason": reason}


def _latest_applied(store):
    """Most recent applied change per pattern, and the number of changes per pattern."""
    latest, total = {}, {}
    for cid, pid, applied in store.connection.execute(
        "SELECT id,pattern_id,applied_at FROM changes WHERE pattern_id IS NOT NULL ORDER BY id"
    ):
        total[pid] = total.get(pid, 0) + 1
        when = _timestamp(applied)
        if when and (pid not in latest or when >= latest[pid][1]):
            latest[pid] = (cid, when)
    return {pid: cid for pid, (cid, _) in latest.items()}, total


def pattern_effects(store, config, *, now=None) -> dict:
    """Per pattern: its latest measurement and whether it is a candidate again (11.2)."""
    latest, total = _latest_applied(store)
    result = {}
    for pid, cid in latest.items():
        measurement = effect_measurement(store, config, cid, now=now)
        recur = (
            measurement["status"] == "measured"
            and measurement["after"]["sessions"] >= config.thresholds.recurrence_sessions
            and measurement["after_rate"] >= measurement["before_rate"]
            and total[pid] < config.thresholds.max_changes_per_pattern
        )
        result[pid] = {"measurement": measurement, "recandidate": recur, "changes": total[pid]}
    return result


def recandidates(store, config, *, now=None) -> list[dict]:
    return [
        {
            "pattern_id": pid,
            "earlier_change": e["measurement"]["change_id"],
            "before_rate": e["measurement"]["before_rate"],
            "after_rate": e["measurement"]["after_rate"],
            "changes": e["changes"],
        }
        for pid, e in sorted(pattern_effects(store, config, now=now).items())
        if e["recandidate"]
    ]


def session_distribution(store) -> dict:
    """Distinct interactions per pattern, and how many patterns have each count."""
    per_pattern = {}
    for row in available_observations(store):
        if row["pattern_id"] and not row["imported"]:
            per_pattern.setdefault(row["pattern_id"], set()).add(interaction_key(row))
    counts = {pid: len(keys) for pid, keys in sorted(per_pattern.items())}
    histogram = {}
    for n in counts.values():
        histogram[n] = histogram.get(n, 0) + 1
    return {"per_pattern": counts, "histogram": dict(sorted(histogram.items()))}


def replay_versus_recurrence(store, config, *, now=None) -> list[dict]:
    """Replay outcome per change next to whether its pattern recurred afterwards."""
    result = []
    for batch in _rows(
        store.connection.execute(
            "SELECT b.change_id,b.status FROM replay_batches b ORDER BY b.change_id"
        )
    ):
        replayed = _rows(
            store.connection.execute(
                "SELECT r.judgement FROM replays r JOIN sessions_seen s "
                "ON s.source=r.source AND s.session_id=r.session_id "
                "WHERE r.change_id=? AND r.baseline_recurs=1 AND s.forgotten=0",
                (batch["change_id"],),
            )
        )
        recurs = sum(1 for r in replayed if r["judgement"] in {"recurs", "unclear"})
        measurement = effect_measurement(store, config, batch["change_id"], now=now)
        measured = measurement["status"] == "measured"
        result.append(
            {
                "change_id": batch["change_id"],
                "replay_status": batch["status"],
                "replayed_sessions": len(replayed),
                "replay_recurs_share": recurs / len(replayed) if replayed else None,
                "later_sessions": measurement.get("after", {}).get("sessions"),
                "later_observations": measurement.get("after", {}).get("observations"),
                "later_recurred": measurement["after_rate"] >= measurement["before_rate"]
                if measured
                else None,
            }
        )
    return result


def _pattern_key(store, pattern_id):
    row = store.connection.execute("SELECT key FROM patterns WHERE id=?", (pattern_id,)).fetchone()
    return row[0] if row else pattern_id


def _rate(value):
    return "" if value is None else f"{value:.2f}"


def render_effect(store, config, *, now=None) -> str:
    """Report section body; every stored value is escaped."""
    now = now or datetime.now(UTC)
    latest, _ = _latest_applied(store)
    rows = []
    for cid in sorted(latest.values()):
        m = effect_measurement(store, config, cid, now=now)
        rows.append(
            (
                m["change_id"],
                _pattern_key(store, m["pattern_id"]),
                m["status"] + (f": {m['reason']}" if "reason" in m else ""),
                f"{m['before']['observations']} obs / {m['before']['sessions']} sessions",
                f"{m['after']['observations']} obs / {m['after']['sessions']} sessions",
                f"{_rate(m.get('before_rate'))} → {_rate(m.get('after_rate'))}"
                if m["status"] == "measured"
                else "",
            )
        )
    if not rows:
        return '<p class="muted">No applied changes to measure.</p>'
    body = (
        f"<p>Raw counts before and after each change was applied, shown as {LABEL}; "
        "they are not proof that the change had an effect.</p>"
        + _table(("Change", "Pattern", "Result", "Before", "After", "Rate per session"), rows)
    )
    again = recandidates(store, config, now=now)
    body += "<h3>Candidates again</h3>" + (
        _table(
            ("Pattern", "Earlier change", "Before rate", "After rate"),
            [
                (
                    r["pattern_id"],
                    r["earlier_change"],
                    _rate(r["before_rate"]),
                    _rate(r["after_rate"]),
                )
                for r in again
            ],
        )
        if again
        else '<p class="muted">No pattern recurs at or above its pre-change rate.</p>'
    )
    distribution = session_distribution(store)["histogram"]
    calibration = replay_versus_recurrence(store, config, now=now)
    body += (
        "<h3>Calibration</h3><p>Patterns by number of distinct sessions: "
        + _escape(", ".join(f"{n} sessions: {c}" for n, c in distribution.items()) or "none")
        + "</p>"
        + _table(
            ("Change", "Replay", "Replay recurrence", "Later sessions", "Recurred later"),
            [
                (
                    r["change_id"],
                    r["replay_status"],
                    _rate(r["replay_recurs_share"]),
                    "" if r["later_sessions"] is None else r["later_sessions"],
                    {None: "insufficient evidence", True: "yes", False: "no"}[r["later_recurred"]],
                )
                for r in calibration
            ],
        )
    )
    return body

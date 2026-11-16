"""Self-contained dream reports from durable, redacted run records.

The page shell and CSS are vendored from agentic-coding/scripts/review_html at
5d07d2f7f31675709f57b35579f72058e846fa67. Upstream declares no licence; source,
modifications and the actual licence status are recorded in UPSTREAM.txt.
"""

import html
import json
import logging
import os
import re
import tempfile
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dreaming.config import ConfigError, require_external
from dreaming.redact import redact

from .css import CSS
from .template import PAGE_TEMPLATE

logger = logging.getLogger(__name__)


def _escape(value):
    return html.escape(redact("" if value is None else str(value)), quote=True)


def _json(value):
    return _escape(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _rows(store, sql, parameters=()):
    cursor = store.connection.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _run(store, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ConfigError("Invalid run identifier")
    rows = _rows(store, "SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        raise ConfigError(f"Unknown run: {run_id}")
    return rows[0]


def _table(headings, rows):
    if not rows:
        return '<p class="muted">None recorded.</p>'
    head = "".join(f"<th>{_escape(h)}</th>" for h in headings)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(v)}</td>" for v in row) + "</tr>" for row in rows
    )
    return f'<table class="findings"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _statistics(store, run):
    rid = run["id"]
    stats, budget = json.loads(run["statistics"]), json.loads(run["budget"])
    sessions = _rows(
        store,
        "SELECT s.source,s.session_id,s.state,s.reason,count(DISTINCT c.task_key) AS parts "
        "FROM sessions_seen s JOIN session_inputs i USING(source,session_id) "
        "JOIN calls c ON c.id=i.call_id JOIN steps t ON t.run_id=c.run_id AND t.key=c.task_key "
        "WHERE c.run_id=? AND c.role='reader' AND t.phase='Read' "
        "GROUP BY s.source,s.session_id,s.state,s.reason ORDER BY s.source,s.session_id",
        (rid,),
    )
    changes = _rows(
        store, "SELECT status,count(*) AS n FROM changes WHERE run_id=? GROUP BY status", (rid,)
    )
    change_counts = {row["status"]: row["n"] for row in changes}
    accepted, patterns = store.connection.execute(
        "SELECT count(*),count(DISTINCT pattern_id) FROM observations WHERE run_id=?", (rid,)
    ).fetchone()
    values = [
        ("Sessions read", stats.get("sessions_read", sum(s["state"] == "read" for s in sessions))),
        ("Sessions split", stats.get("sessions_split", sum(s["parts"] > 1 for s in sessions))),
        (
            "Sessions skipped",
            stats.get("sessions_skipped", sum(s["state"] == "skipped" for s in sessions)),
        ),
        (
            "Sessions failed",
            stats.get("sessions_failed", sum(s["state"] == "failed" for s in sessions)),
        ),
        ("Observations accepted", stats.get("observations_accepted", accepted)),
        ("Observations rejected", stats.get("observations_rejected", 0)),
        ("Patterns", patterns),
        ("Changes applied", change_counts.get("applied", 0)),
        ("Changes waiting", change_counts.get("waiting", 0) + change_counts.get("proposed", 0)),
        ("Changes withheld", change_counts.get("withheld", 0)),
        ("Run status", run["status"]),
        ("Partial", "yes" if run["status"] == "partial" else "no"),
    ]
    usage = _rows(
        store,
        "SELECT backend,model,sum(tokens_in) AS tokens_in,sum(tokens_out) AS tokens_out,"
        "sum(seconds) AS seconds,max(usage_estimated) AS estimated FROM calls "
        "WHERE run_id=? GROUP BY backend,model ORDER BY backend,model",
        (rid,),
    )
    tokens = sum(row["tokens_in"] + row["tokens_out"] for row in usage)
    values += [
        ("Model tokens used", tokens),
        ("Token limit", budget.get("max_tokens", "unavailable")),
        ("Active seconds", budget.get("active_seconds", stats.get("active_seconds", 0))),
    ]
    result = _table(("Statistic", "Value"), values)
    result += "<h3>Backend and model use</h3>" + _table(
        ("Backend", "Model", "Input tokens", "Output tokens", "Seconds", "Usage"),
        [
            (
                r["backend"],
                r["model"] or "unavailable",
                r["tokens_in"],
                r["tokens_out"],
                round(r["seconds"], 3),
                "estimated" if r["estimated"] else "measured",
            )
            for r in usage
        ],
    )
    issues = [
        (s["source"], s["session_id"], s["state"], s["reason"])
        for s in sessions
        if s["state"] != "read" or s["reason"]
    ]
    issues.extend(
        (
            s.get("source", ""),
            s.get("session_id", s.get("id", "")),
            s.get("state", "skipped"),
            s.get("reason", ""),
        )
        for s in stats.get("selection_skipped", [])
        if isinstance(s, dict)
    )
    # Unavailable optional sources and integrations are reported, never blocking (14.4).
    warnings = [
        (d.get("source", ""), d["code"], d.get("message", ""))
        for d in stats.get("selection_diagnostics", [])
        if isinstance(d, dict) and isinstance(d.get("code"), str)
    ]
    if warnings:
        result += "<h3>Source and integration warnings</h3>" + _table(
            ("Source", "Code", "Message"), warnings
        )
    if issues:
        result += "<h3>Session outcomes</h3>" + _table(
            ("Source", "Session", "State", "Reason"), issues
        )
    return result


def _knowledge(store, rid):
    rows = _rows(
        store, "SELECT * FROM knowledge_revisions WHERE run_id=? ORDER BY item_id,version", (rid,)
    )
    if not rows:
        return '<p class="muted">No retained knowledge revisions in this dream.</p>'
    result = []
    for row in rows:
        result.append(
            '<article class="change-card">'
            f"<h3>{_escape(row['summary'])}</h3>"
            f"<p><code>{_escape(row['item_id'])}</code> · version {row['version']} · "
            f"{_escape(row['kind'])} · {_escape(row['status'])}</p>"
            f"<p>Confidence: {_escape(row['confidence'])}. "
            f"{_escape(row['confidence_basis'])}</p>"
            f"<p>Pending reassessment: {'yes' if row['pending_reassessment'] else 'no'}</p>"
            f"<pre>{_json(json.loads(row['scope']))}</pre></article>"
        )
    return "".join(result)


def _learning(store, rid):
    rows = _rows(store, "SELECT * FROM learning WHERE run_id=? ORDER BY id", (rid,))
    if not rows:
        return '<p class="muted">No learning recommendations in this dream.</p>'
    return "".join(
        '<article class="change-card">'
        f"<h3>{_escape(row['next_step'])}</h3><p>{_escape(row['outcome'])}</p>"
        f"<p><code>{_escape(row['id'])}</code> · {_escape(row['disposition'])}</p>"
        f"<p>{_escape(row['reason'])} {_escape(row['deferred_until'])}</p>"
        f"<pre>{_json(json.loads(row['scope']))}</pre></article>"
        for row in rows
    )


def _changes(store, rid):
    rows = _rows(store, "SELECT * FROM changes WHERE run_id=? ORDER BY repo,id", (rid,))
    if not rows:
        return '<p class="muted">No repository changes.</p>'
    return _table(
        ("Repository", "Change", "Branch", "State", "Evidence", "Reason", "Ranking signals"),
        [
            (
                r["repo"],
                r["id"],
                r["branch"],
                r["status"],
                ", ".join(json.loads(r["evidence"])),
                r["reason"],
                json.dumps(json.loads(r["rank_signals"]), sort_keys=True),
            )
            for r in rows
        ],
    )


def _outcomes(store, rid):
    rows = _rows(
        store,
        "SELECT operation_key,disposition,reason FROM review_decisions "
        "WHERE run_id=? ORDER BY operation_key",
        (rid,),
    )
    result = _table(("Candidate", "Disposition", "Reason"), [tuple(r.values()) for r in rows])
    steps = _rows(
        store, "SELECT key,phase,state,reason,result FROM steps WHERE run_id=? ORDER BY key", (rid,)
    )
    deferred = []
    for step in steps:
        try:
            output = json.loads(step["result"]) if step["result"] else {}
        except ValueError:
            output = {}
        output = output if isinstance(output, dict) else {}
        reason = step["reason"] or output.get("deferred") or output.get("omitted")
        if reason:
            deferred.append((step["key"], step["phase"], step["state"], reason))
    if deferred:
        result += "<h3>Step outcomes</h3>" + _table(("Step", "Phase", "State", "Reason"), deferred)
    return result


def _unmerged(store, rid):
    rows = _rows(
        store,
        "SELECT DISTINCT repo,branch,run_id,status,reason FROM changes WHERE run_id<>? "
        "AND branch IS NOT NULL AND status IN ('proposed','waiting','withheld') "
        "ORDER BY repo,branch,run_id,status,reason",
        (rid,),
    )
    return _table(
        ("Repository", "Branch", "Dream", "State", "Reason"), [tuple(r.values()) for r in rows]
    )


def _configuration(store, rid):
    rows = _rows(
        store, "SELECT id,repo,edits,status FROM changes WHERE run_id=? ORDER BY id", (rid,)
    )
    paths = []
    for row in rows:
        for edit in json.loads(row["edits"]):
            path = edit.get("path", "")
            if re.search(
                r"(?i)(?:^|/)(?:Makefile|AGENTS\.md|CLAUDE\.md|instructions|rules|"
                r"config[^/]*|settings[^/]*|\.claude|\.codex)(?:/|$)|\.(?:toml|json|ya?ml)$",
                path,
            ):
                paths.append((row["repo"], path, row["id"], row["status"]))
    return _table(("Repository", "Path", "Change", "State"), paths)


def _source(source):
    title = _escape(source.get("title") or source.get("link") or source.get("url") or "Source")
    link = source.get("link", source.get("url", ""))
    try:
        parsed = urlsplit(link) if isinstance(link, str) else None
        allowed = parsed and parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        allowed = False
    if allowed:
        title = f'<a href="{_escape(link)}" rel="noreferrer">{title}</a>'
    retrieved = source.get("retrieved_at", source.get("retrieval_date"))
    return title + (f" · retrieved {_escape(retrieved)}" if retrieved else "")


def _currency(store, rid):
    rows = _rows(store, "SELECT * FROM currency_findings WHERE run_id=? ORDER BY id", (rid,))
    skipped = _rows(
        store,
        "SELECT url,reason FROM currency_pages "
        "WHERE run_id=? AND status='unavailable' ORDER BY url",
        (rid,),
    )
    if not rows and not skipped:
        return '<p class="muted">No currency notes or changes in this dream.</p>'
    cards = "".join(
        '<article class="change-card">'
        f"<h3>{_escape(row['claim'])}</h3><p>{_escape(row['target'])} · "
        f"{_escape(row['status'])}</p><ul>"
        + "".join(f"<li>{_source(s)}</li>" for s in json.loads(row["sources"]))
        + "</ul></article>"
        for row in rows
    )
    if skipped:
        cards += (
            "<p>Sources skipped:</p><ul>"
            + "".join(
                f"<li>{_source(dict(title=r['url'], url=r['url']))} · {_escape(r['reason'])}</li>"
                for r in skipped
            )
            + "</ul>"
        )
    return cards


def _replays(store, rid):
    batches = _rows(
        store,
        "SELECT b.change_id,b.authoring_id,b.status,b.reason FROM replay_batches b "
        "JOIN changes c ON c.id=b.change_id WHERE c.run_id=? ORDER BY b.change_id,b.authoring_id",
        (rid,),
    )
    if not batches:
        return '<p class="muted">No behavioural replays in this dream.</p>'
    rows = []
    for batch in batches:
        outcomes = _rows(
            store,
            "SELECT r.* FROM replays r JOIN sessions_seen s "
            "ON s.source=r.source AND s.session_id=r.session_id "
            "WHERE r.change_id=? AND r.authoring_id=? AND s.forgotten=0",
            (batch["change_id"], batch["authoring_id"]),
        )
        baseline = [r for r in outcomes if r["baseline_recurs"]]
        rows.append(
            (
                batch["change_id"],
                batch["status"],
                len(baseline),
                sum(r["judgement"] != "resolved" for r in baseline),
                sum(r["tokens"] for r in outcomes),
                ", ".join(
                    sorted({r["backend"] + "/" + (r["model"] or "unavailable") for r in outcomes})
                ),
                batch["reason"],
            )
        )
    return (
        "<p>These paired replay results show association, not proof of improvement. "
        "Only recurring baselines count; unclear changed outcomes count as recurrence.</p>"
        + _table(
            (
                "Change",
                "Result",
                "Recurring baselines",
                "Recurs with change",
                "Tokens",
                "Backend/model",
                "Reason",
            ),
            rows,
        )
    )


def render_report(store, run_id: str, *, sections: dict[str, str] | None = None) -> str:
    """Render durable state; ``sections`` supplies trusted internal extension HTML."""
    run = _run(store, run_id)
    forgotten = _rows(
        store,
        "SELECT source,session_id,availability FROM sessions_seen WHERE forgotten=1 "
        "ORDER BY source,session_id",
    )
    content = {
        "statistics": ("Summary and statistics", _statistics(store, run)),
        "knowledge": ("Retained knowledge revisions", _knowledge(store, run_id)),
        "learning": ("Learning recommendations", _learning(store, run_id)),
        "changes": ("Repository changes", _changes(store, run_id)),
        "replays": ("Behavioural replay association", _replays(store, run_id)),
        "outcomes": ("Deferred and discarded candidates", _outcomes(store, run_id)),
        "effect": ("Effect measurement", '<p class="muted">No effect measurements available.</p>'),
        "inventory": ("Tooling inventory", '<p class="muted">No tooling inventory available.</p>'),
        "unmerged": ("Unmerged branches from earlier dreams", _unmerged(store, run_id)),
        "configuration": ("Configuration, checks and restrictions", _configuration(store, run_id)),
        "forgotten": (
            "Forgotten sessions",
            _table(("Source", "Session", "Availability"), [tuple(r.values()) for r in forgotten]),
        ),
        "currency": ("Currency notes and changes", _currency(store, run_id)),
    }
    for key, body in (sections or {}).items():
        if key not in content or not isinstance(body, str):
            raise ValueError(f"Unknown or invalid report section: {key}")
        content[key] = (content[key][0], body)
    toc = (
        '<nav class="toc" aria-label="Report sections"><ul>'
        + "".join(
            f'<li><a href="#{key}">{_escape(title)}</a></li>' for key, (title, _) in content.items()
        )
        + "</ul></nav>"
    )
    body = "".join(
        f'<section id="{key}"><h2>{_escape(title)}</h2>{body}</section>'
        for key, (title, body) in content.items()
    )
    placeholders = dict.fromkeys(PAGE_TEMPLATE.get_identifiers(), "")
    placeholders.update(
        title_plain=f"Dream {_escape(run_id)}",
        title_html=f"Dream {_escape(run_id)}",
        repo_name="Dreaming",
        metrics_chips=f'<span class="chip">{_escape(run["status"])}</span>',
        css=CSS,
        subtitle=f"Requested by {_escape(run['requester'])} · "
        f"{_escape(run['started_at'])} · revision {_escape(run['revision'])}",
        verdict_card=f'<article class="card"><h3>Run outcome</h3>'
        f"<p>{_escape(run['status'])}</p><p>{_escape(run['notice'])}</p></article>",
        toc=toc,
        pr_description_section=body,
        timestamp=_escape(datetime.now(UTC).isoformat()),
        repo_path=_escape(run_id),
    )
    return PAGE_TEMPLATE.substitute(placeholders)


def write_report(
    store,
    run_id: str,
    *,
    open_browser: bool = False,
    sections: dict[str, str] | None = None,
) -> Path:
    """Atomically write a private report; browser failure never fails the dream."""
    run = _run(store, run_id)
    body = render_report(store, run_id, sections=sections)
    directory = require_external(store.path.parent / "reports")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = require_external(directory / f"{run_id}.html")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(body)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    if open_browser and run["requester"] == "user":
        try:
            opened = webbrowser.open(path.as_uri())
        except Exception:
            opened = False
        if not opened:
            logger.warning("Could not open dream report at %s", path)
    return path

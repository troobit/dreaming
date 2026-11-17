"""The report's summary and sections, built from the store (10.2–10.6, 15.2, 16.2).

A section body takes the connection, the run's row and the run's configuration
and returns HTML, or "" to leave the section out. Every stored text is escaped,
and links are rendered only for http and https (16.2).

`inventory` shows the inventory recorded as the run ended, or an empty state
when none was recorded.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from fnmatch import fnmatchcase
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit

from dreaming import adopt, budget
from dreaming import effect as effects
from dreaming import inventory as tooling
from dreaming.backends import mcp
from dreaming.report.shell import card, escape, pill, verdict

Body = Callable[[sqlite3.Connection, sqlite3.Row, dict], str]

# How each way a run ends is shown: label, tone and what it means.
ENDINGS = {
    "complete": ("Complete", "success", "The dream finished."),
    "budget_exhausted": (
        "Budget reached",
        "warning",
        "The dream stopped at its own budget; its completed work is kept.",
    ),
    "partial": (
        "Partial",
        "warning",
        "No backend was left for a role; the completed work is kept and the "
        "dream can be resumed.",
    ),
    "interrupted": (
        "Failed",
        "error",
        "The dream stopped before finishing; resume or discard it.",
    ),
}
# Knowledge kinds in the order shown, each under its own heading, so preferences
# stay apart from learning opportunities (15.2).
KINDS = {
    "preference": "Preferences",
    "lesson": "Lessons",
    "workflow": "Workflow patterns",
    "learning": "Learning opportunities",
}
TONES = {
    "active": "success",
    "authored": "success",
    "contested": "warning",
    "deferred": "warning",
    "dismissed": "error",
    "discarded": "error",
}
CHANGE_TONES = {
    "applied": "success",
    "proposed": "tertiary",
    "waiting": "warning",
    "withheld": "warning",
    "failed": "error",
    "rejected": "error",
}
# Change states that leave a change on its unmerged dream branch (9.14), as
# `review.UNMERGED`.
UNMERGED = ("proposed", "waiting")
# Edit paths that change checks, restrictions or tool lists (10.4), or dreaming's
# own instructions (7.4). An edit does not say what its file configures, so this
# is a heuristic on the path.
CONFIG_PATHS = {
    "checks": (
        "Makefile",
        "*/Makefile",
        "*.mk",
        ".github/workflows/*",
        ".pre-commit-config.yaml",
    ),
    "restrictions": ("*settings.json", "*settings.local.json", "*permissions*"),
    "tool lists": ("*.mcp.json", "*mcp/servers.json", "*config*.toml"),
    "dreaming's instructions": ("instructions/*",),
}
WEB_SCHEMES = ("http", "https")


class Html(str):
    """Markup built here from escaped parts; a table cell of any other type is
    escaped."""


def _cell(value: object) -> str:
    return value if isinstance(value, Html) else escape(value)


def _table(head: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """A table in the review page's style (its `findings` class)."""
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return (
        f'<table class="findings"><thead><tr>{th}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _with_reason(text: str | None, reason: str | None) -> Html:
    """A cell's text with the reason for its state underneath."""
    shown = escape(text)
    if reason:
        shown += f'<br><span class="muted">{escape(reason)}</span>'
    return Html(shown)


def _empty(text: str) -> str:
    return f'<p class="muted">{escape(text)}</p>'


def _count(conn: sqlite3.Connection, sql: str, *args: object) -> int:
    return conn.execute(sql, args).fetchone()[0]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _ids(raw: str | None) -> list:
    return json.loads(raw or "[]")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _of(used: object, limit: object) -> str:
    shown = f"{used:,}" if isinstance(used, int) else str(used)
    return shown if limit is None else f"{shown} of {limit:,}"


def scope_text(raw: str | None) -> str:
    """A scope in words; a scope naming nothing is unresolved, never global (13.2)."""
    scope = json.loads(raw or "{}")
    named = [
        f"{key} {', '.join(scope[key])}"
        for key in ("domains", "projects", "workflows")
        if scope.get(key)
    ]
    if scope.get("unresolved"):
        return "unresolved scope"
    if scope.get("global"):
        return "global"
    return "; ".join(named) or "unresolved scope"


def source_link(title: str | None, link: str | None) -> str:
    """A source's title as text, linked only when its link is http or https; any
    other link is shown as text (16.2)."""
    url = (link or "").strip()
    text = escape(title or url)
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is not None and parts.scheme.lower() in WEB_SCHEMES:
        href = escape(parts.geturl())
        return f'<a href="{href}" rel="noopener noreferrer">{text}</a>'
    if url and url != title:
        return f"{text} <code>{escape(url)}</code>"
    return text


def _sources(raw: str | None, quote_chars: int) -> str:
    """Cited sources: the title, the link, the retrieval date and the passage
    relied on."""
    items = []
    for source in json.loads(raw or "[]"):
        item = source_link(source.get("title"), source.get("link"))
        if source.get("retrieved_on"):
            retrieved = escape(source["retrieved_on"])
            item += f' <span class="muted">retrieved {retrieved}</span>'
        if source.get("passage"):
            passage = escape(_clip(source["passage"], quote_chars))
            item += f' <span class="muted">“{passage}”</span>'
        items.append(f"<li>{item}</li>")
    return f"<ul>{''.join(items)}</ul>" if items else ""


def _findings(conn: sqlite3.Connection, ids: Sequence[str], quote_chars: int) -> str:
    """Research findings by id, each claim with its sources."""
    rows = conn.execute(
        "SELECT finding, sources FROM research_findings"
        " WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(list(ids)),),
    )
    items = "".join(
        f"<li>{escape(r['finding'])}{_sources(r['sources'], quote_chars)}</li>"
        for r in rows
    )
    return f"<ul>{items}</ul>" if items else ""


# The summary above the sections


def ending(run: sqlite3.Row) -> tuple[str, str, str]:
    return ENDINGS.get(run["status"], (run["status"], "warning", ""))


def outcome(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """What the dream retained and what it proposed, counted apart (10.6)."""
    return {
        "revisions": _count(
            conn,
            "SELECT COUNT(*) FROM knowledge_revisions WHERE originating_run = ?",
            run_id,
        ),
        "learning": _count(conn, "SELECT COUNT(*) FROM learning WHERE run = ?", run_id),
        "changes": _count(conn, "SELECT COUNT(*) FROM changes WHERE run = ?", run_id),
    }


def no_changes(counts: dict[str, int]) -> str:
    """The statement for a dream without repository changes; it names what the
    dream retained rather than calling that no result (10.6)."""
    if counts["revisions"] or counts["learning"]:
        return (
            "No repository changes. This dream's result is the "
            f"{_plural(counts['revisions'], 'knowledge revision')} and "
            f"{_plural(counts['learning'], 'learning recommendation')} it retained."
        )
    return (
        "No repository changes, and no knowledge or learning recommendation "
        "was retained."
    )


def subtitle(run: sqlite3.Row) -> str:
    kind = "Currency review" if run["kind"] == "currency" else "Dream"
    ended = f"ended {run['ended_at']}" if run["ended_at"] else "not ended"
    return (
        f"{kind} started {run['started_at']}, {ended}, requested by {run['requester']}"
    )


def metrics(conn: sqlite3.Connection, run: sqlite3.Row) -> list[tuple[str, object]]:
    rid = run["id"]
    read = _count(
        conn,
        "SELECT COUNT(*) FROM steps"
        " WHERE run = ? AND phase = 'read' AND state = 'done'",
        rid,
    )
    observed = _count(conn, "SELECT COUNT(*) FROM observations WHERE run = ?", rid)
    return [
        ("Status", ending(run)[0]),
        ("Sessions read", read),
        ("Observations", observed),
        ("Tokens", f"{budget.tokens_used(conn, rid):,}"),
    ]


def cards(conn: sqlite3.Connection, run: sqlite3.Row) -> list[str]:
    """How the dream ended, and what it retained and proposed, each counted on its
    own line (10.6)."""
    label, tone, meaning = ending(run)
    detail = f'<p class="muted">{escape(meaning)}</p>' if meaning else ""
    if run["error"]:
        detail += f"<p>{escape(run['error'])}</p>"
    counts = outcome(conn, run["id"])
    revisions = _plural(counts["revisions"], "knowledge revision")
    learning = _plural(counts["learning"], "learning recommendation")
    changes = (
        f'<a href="#changes">{_plural(counts["changes"], "repository change")}</a>'
        if counts["changes"]
        else escape(no_changes(counts))
    )
    items = (
        f'<li><a href="#knowledge">{revisions} retained</a></li>'
        f'<li><a href="#learning">{learning}</a></li>'
        f"<li>{changes}</li>"
    )
    return [
        card("Outcome", f"<p>{verdict(label, tone)}</p>{detail}"),
        card("Retained and proposed", f"<ul>{items}</ul>"),
    ]


# Sections


def statistics(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Run statistics (10.3): sessions read, split, skipped and failed with
    reasons; observations accepted and rejected; patterns; changes; budget used;
    time and tokens per backend and model; and whether the dream was partial."""
    rid = run["id"]
    reads = conn.execute(
        "SELECT key, state, reason, input_ref FROM steps"
        " WHERE run = ? AND phase = 'read' ORDER BY seq",
        (rid,),
    ).fetchall()
    states = Counter(step["state"] for step in reads)
    # Every reader call is a part (`read:…#p1`, `#p2`, …), so a second part means
    # the session was split (2.6).
    split = _count(
        conn,
        "SELECT COUNT(DISTINCT step_key) FROM calls"
        " WHERE run = ? AND step_key LIKE 'read:%#p2'",
        rid,
    )
    rejections = conn.execute(
        "SELECT what, reason, COUNT(*) AS n FROM rejections WHERE run = ?"
        " GROUP BY what, reason ORDER BY what, n DESC, reason",
        (rid,),
    ).fetchall()
    changes = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM changes WHERE run = ? GROUP BY status",
            (rid,),
        ).fetchall()
    )
    limits = json.loads(run["budget"] or "{}")
    estimated = _count(
        conn, "SELECT COUNT(*) FROM calls WHERE run = ? AND usage_estimated = 1", rid
    )
    tokens = _of(budget.tokens_used(conn, rid), limits.get("tokens"))
    minutes = _of(f"{run['active_seconds'] / 60:.1f}", limits.get("wall_clock_minutes"))
    rows = [
        ("Sessions read", _of(states["done"], limits.get("sessions"))),
        ("Sessions split into parts", split),
        ("Sessions skipped", states["skipped"]),
        ("Sessions failed", states["failed"]),
    ]
    if states["pending"]:
        rows.append(("Sessions not reached", states["pending"]))
    rows += [
        (
            "Observations accepted",
            _count(conn, "SELECT COUNT(*) FROM observations WHERE run = ?", rid),
        ),
        (
            "Observations rejected",
            sum(r["n"] for r in rejections if r["what"] == "observation"),
        ),
        (
            "New patterns",
            _count(conn, "SELECT COUNT(*) FROM patterns WHERE created_run = ?", rid),
        ),
        (
            "Patterns with new evidence",
            _count(
                conn,
                "SELECT COUNT(DISTINCT pattern_id) FROM observations WHERE run = ?",
                rid,
            ),
        ),
        (
            "Changes proposed / applied / waiting / withheld",
            " / ".join(
                str(changes.get(status, 0))
                for status in ("proposed", "applied", "waiting", "withheld")
            ),
        ),
        ("Tokens used", tokens + (", partly estimated" if estimated else "")),
        ("Running time (minutes)", minutes),
        ("Partial", "yes" if run["status"] == "partial" else "no"),
    ]
    parts = [_table(("Measure", "Value"), rows)]
    stopped = [step for step in reads if step["state"] in ("skipped", "failed")]
    if stopped:
        parts += [
            "<h3>Sessions skipped or failed</h3>",
            _table(
                ("Session", "State", "Reason"),
                ((_session(s), s["state"], s["reason"]) for s in stopped),
            ),
        ]
    if rejections:
        parts += [
            "<h3>Rejected observations and operations</h3>",
            _table(
                ("What", "Reason", "Count"),
                ((r["what"], r["reason"], r["n"]) for r in rejections),
            ),
        ]
    usage = budget.usage_by_backend(conn, rid)
    if usage:
        parts += [
            "<h3>Time and tokens by backend and model</h3>",
            _table(
                ("Backend", "Model", "Calls", "Tokens in", "Tokens out", "Seconds"),
                (
                    (
                        u["backend"],
                        u["model"],
                        u["calls"],
                        f"{u['tokens_in']:,}",
                        f"{u['tokens_out']:,}",
                        f"{u['seconds']:.1f}",
                    )
                    for u in usage
                ),
            ),
        ]
    return "\n".join(parts)


def _session(step: sqlite3.Row) -> str:
    ref = json.loads(step["input_ref"] or "{}")
    return f"{ref.get('source', '?')}:{ref.get('id', step['key'])}"


def knowledge(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Knowledge revisions the dream retained (10.6, 13.8), by kind, with their
    status (contested included) and reason."""
    rows = conn.execute(
        "SELECT * FROM knowledge_revisions WHERE originating_run = ?", (run["id"],)
    ).fetchall()
    if not rows:
        return _empty("This dream retained no knowledge revision.")
    order = list(KINDS)

    def position(row: sqlite3.Row) -> tuple:
        kind = row["kind"]
        rank = order.index(kind) if kind in order else len(order)
        return rank, kind, row["item_id"], row["version"]

    parts = []
    for kind, group in groupby(sorted(rows, key=position), key=lambda r: r["kind"]):
        parts.append(f"<h3>{escape(KINDS.get(kind, kind))}</h3>")
        parts.append(
            _table(
                ("Item", "Status", "Knowledge", "Scope", "Confidence", "Evidence"),
                (_revision(r) for r in group),
            )
        )
    return "\n".join(parts)


def _revision(r: sqlite3.Row) -> tuple:
    basis = r["confidence_basis"]
    return (
        Html(f"<code>{escape(r['item_id'])}</code> v{r['version']}"),
        Html(pill(r["status"], TONES.get(r["status"], "tertiary"))),
        _with_reason(r["content"], r["reason"]),
        scope_text(r["scope"]),
        f"{r['confidence']}: {basis}" if basis else r["confidence"],
        f"{len(_ids(r['supporting']))} supporting, "
        f"{len(_ids(r['contradicting']))} contradicting",
    )


def learning(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Learning recommendations (15.1, 15.2), apart from preferences and
    automation, each with its researched resources shown apart from the observed
    evidence."""
    rows = conn.execute(
        "SELECT * FROM learning WHERE run = ? ORDER BY created_at, id", (run["id"],)
    ).fetchall()
    if not rows:
        return _empty("This dream made no learning recommendation.")
    quote_chars = cfg["thresholds"]["quote_chars"]
    return "\n".join(_recommendation(conn, row, quote_chars) for row in rows)


def _recommendation(
    conn: sqlite3.Connection, row: sqlite3.Row, quote_chars: int
) -> str:
    evidence = _ids(row["evidence"])
    research = _findings(conn, _ids(row["research"]), quote_chars)
    parts = [
        f"<h3>{escape(row['summary'])}</h3>",
        f'<p class="muted"><code>{escape(row["id"])}</code> · '
        f"{escape(scope_text(row['scope']))} · "
        f"{pill(row['disposition'], 'tertiary')}</p>",
        f"<p><strong>Next step.</strong> {escape(row['next_step'])}</p>",
        f"<p><strong>Helps achieve.</strong> {escape(row['outcome'])}</p>",
        f"<p><strong>Observed evidence.</strong> "
        f"{_plural(len(evidence), 'observation')}: "
        f"<code>{escape(', '.join(evidence))}</code></p>",
    ]
    if research:
        parts.append(
            '<div class="callout callout-rationale">'
            f"<strong>Researched, not observed.</strong>{research}</div>"
        )
    return '<div class="change-card">' + "\n".join(parts) + "</div>"


def changes(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Repository changes (10.2). A dream without any says so, naming what it
    retained instead (10.6)."""
    counts = outcome(conn, run["id"])
    if not counts["changes"]:
        return f"<p>{escape(no_changes(counts))}</p>"
    return repository_changes(conn, run, cfg)


def repository_changes(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Each repository's changes with their evidence, whether each was applied
    and why not, and the commands to merge or revert them, each followed by the
    repository's generate and deploy commands (9.11, 9.15, 10.2). A withheld
    change names its saved patch instead of a commit (9.12)."""
    parts = []
    for found in adopt.repository_commands(conn, cfg, run["id"]):
        rows = [
            (
                Html(
                    _with_reason(c["summary"] or c["id"], c["reason"])
                    + _replay_note(conn, c["id"])
                ),
                Html(pill(c["status"], CHANGE_TONES.get(c["status"], "tertiary"))),
                Html(
                    " ".join(f"<code>{escape(e)}</code>" for e in _ids(c["evidence"]))
                ),
                Html(f"<code>{escape(c['commit_sha'][:12])}</code>")
                if c["commit_sha"]
                else c["patch_path"] or "",
            )
            for c in found["changes"]
        ]
        commands = "".join(
            f"<p>{escape(label)}:</p>"
            f'<div class="file-diff"><pre><code>{escape(chr(10).join(lines))}'
            "</code></pre></div>"
            for label, lines in found["commands"]
        )
        parts.append(
            f"<h3>{escape(found['repo'])} "
            f'<span class="muted">{escape(found["state"])}</span></h3>'
            + _table(("Change", "State", "Evidence", "Commit or patch"), rows)
            + commands
        )
    return "".join(parts)


def _replay_note(conn: sqlite3.Connection, change_id: str) -> str:
    """A change's replay in counts, shown as association rather than proof
    (9.18)."""
    rows = conn.execute(
        "SELECT recurs_without, judgement, tokens FROM replays WHERE change_id = ?",
        (change_id,),
    ).fetchall()
    if not rows:
        return ""
    replayed = [r for r in rows if r["recurs_without"]]
    recurs = sum(r["judgement"] != "resolved" for r in replayed)
    return (
        '<br><span class="muted">Replay, an association rather than proof: of '
        f"{_plural(len(rows), 'session')} run, the problem recurred without the "
        f"change in {len(replayed)} and, of those, with it in {recurs}; "
        f"{sum(r['tokens'] for r in rows):,} tokens.</span>"
    )


def candidates(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Candidates by rank with their signals (11.3), why any was deferred or
    discarded (10.6, 11.6), and skill steps deferred for needing input (9.3)."""
    rows = conn.execute(
        "SELECT * FROM candidates WHERE run = ?"
        " ORDER BY position IS NULL, position, id",
        (run["id"],),
    ).fetchall()
    if not rows:
        return ""
    return _table(
        ("Rank", "Candidate", "Proposal", "Signals", "Status", "Reason"),
        (_candidate(r) for r in rows),
    )


def _candidate(r: sqlite3.Row) -> tuple:
    s = json.loads(r["signals"] or "{}")
    signals = (
        f"{s.get('sessions', 0)} sessions, {s.get('projects', 0)} projects, "
        f"recency {s.get('recency', 0)}"
    )
    what = escape(r["summary"])
    if r["repo"]:
        what += f' <span class="muted">{escape(r["repo"])}</span>'
    for step in json.loads(r["deferred_steps"] or "[]"):
        what += (
            f"<br>Deferred: {escape(step.get('skill'))} {escape(step.get('step'))}"
            f" ({escape(step.get('reason'))})"
        )
    return (
        r["position"],
        Html(what),
        r["proposal"],
        signals,
        Html(pill(r["status"], TONES.get(r["status"], "tertiary"))),
        r["reason"],
    )


def research(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Research questions with their findings, and the sources that could not be
    read and were skipped (4.6, 16.2). Findings are not evidence about the user
    (13.6)."""
    questions = conn.execute(
        "SELECT * FROM questions WHERE run = ? ORDER BY created_at, id", (run["id"],)
    ).fetchall()
    if not questions:
        return ""
    quote_chars = cfg["thresholds"]["quote_chars"]
    parts = [
        '<p class="muted">Research findings explain or support candidates; '
        "they are not evidence about you.</p>"
    ]
    for q in questions:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM research_findings WHERE question_id = ?", (q["id"],)
            )
        ]
        parts.append(f"<h3>{escape(q['question'])}</h3>")
        parts.append(_findings(conn, ids, quote_chars) or _empty("No finding."))
        unavailable = json.loads(q["unavailable"] or "[]")
        if unavailable:
            items = "".join(
                f"<li>{escape(u.get('source'))}: {escape(u.get('reason'))}</li>"
                for u in unavailable
            )
            parts.append(
                '<div class="callout callout-warning">'
                f"<strong>Could not be read, skipped.</strong><ul>{items}</ul></div>"
            )
    return "\n".join(parts)


def currency(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Currency notes and the currency findings proposed as changes (16.2–16.5),
    apart from learning recommendations and from evidence about the user, with
    the sources that could not be read and the targets not reviewed."""
    rid = run["id"]
    rows = conn.execute(
        "SELECT * FROM currency_findings WHERE run = ? OR id IN"
        " (SELECT currency_finding_id FROM candidates WHERE run = ?)"
        " ORDER BY status != 'note', target, id",
        (rid, rid),
    ).fetchall()
    unread = conn.execute(
        "SELECT link, error FROM fetched_pages WHERE run = ? AND error IS NOT NULL"
        " ORDER BY link",
        (rid,),
    ).fetchall()
    missed = conn.execute(
        "SELECT input_ref, reason FROM steps WHERE run = ? AND phase = 'research'"
        " AND reason IS NOT NULL ORDER BY seq",
        (rid,),
    ).fetchall()
    if run["kind"] != "currency" and not rows:
        return ""
    quote_chars = cfg["thresholds"]["quote_chars"]
    parts = [
        '<p class="muted">Currency findings compare skills, rules and workflows '
        "with current sources. They are not learning recommendations and not "
        "evidence about you; a note is kept for reference, a proposed finding "
        "became a repository change.</p>"
    ]
    if rows:
        parts.append(
            _table(
                ("Target", "Finding", "Status", "Sources"),
                (
                    (
                        r["target"],
                        _with_reason(r["claim"], r["reason"]),
                        Html(pill(r["status"], TONES.get(r["status"], "tertiary"))),
                        Html(_sources(r["sources"], quote_chars)),
                    )
                    for r in rows
                ),
            )
        )
    else:
        parts.append(_empty("This review kept no finding."))
    if missed and run["kind"] == "currency":
        parts += [
            "<h3>Targets not reviewed</h3>",
            _table(("Target", "Reason"), ((m[0], m[1]) for m in missed)),
        ]
    if unread:
        items = "".join(
            f"<li>{escape(u['link'])}: {escape(u['error'])}</li>" for u in unread
        )
        parts.append(
            '<div class="callout callout-warning">'
            f"<strong>Could not be read, skipped.</strong><ul>{items}</ul></div>"
        )
    return "\n".join(parts)


def effect(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Each applied change's pattern before and after it was applied, as raw
    counts labelled association (11.1), and the calibration statistics: distinct
    sessions per pattern and replays against later recurrence (design, Risks
    and Assumptions)."""
    found = effects.measured(conn, cfg)
    parts = [
        '<p class="muted">Observations of each changed pattern and sessions read '
        "under its lens, before and after the change was applied. They show an "
        f"{effects.LABEL}, that the change made any difference.</p>"
    ]
    if found:
        parts.append(
            _table(
                ("Change", "Applied", "Before", "After", "Result"),
                (
                    (
                        _with_reason(e.summary, f"pattern {e.pattern_id}"),
                        e.applied_at[:10]
                        + (f", reverted {e.reverted_at[:10]}" if e.reverted_at else ""),
                        effects.observation_counts(
                            e.before_observations, e.before_sessions
                        ),
                        effects.observation_counts(
                            e.after_observations, e.after_sessions
                        ),
                        effects.describe(e),
                    )
                    for e in found
                ),
            )
        )
    else:
        parts.append(_empty("No change has been applied yet."))
    spread = effects.session_distribution(conn)
    if spread:
        parts += [
            "<h3>Distinct sessions per pattern</h3>",
            _table(("Distinct sessions", "Patterns"), spread),
        ]
    replays = effects.replay_against_recurrence(conn, cfg)
    if replays:
        parts += [
            "<h3>Replay against later recurrence</h3>",
            _table(
                ("Change", "Sessions replayed", "Recurred with the change", "Later"),
                (
                    (r["summary"], r["replayed"], r["recurred_with_change"], r["later"])
                    for r in replays
                ),
            ),
        ]
    return "\n".join(parts)


STAGES = {
    "evidence_gathering": "Evidence gathering",
    "reflection": "Reflection",
    "selection": "Candidate selection",
    "adoption": "Adoption",
}


def outcomes(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """What evidence gathering, reflection, candidate selection and adoption came
    to, with the reason for each deferral (11.6)."""
    found = effects.stage_outcomes(conn, run["id"])
    rows = [
        (
            label,
            ", ".join(
                f"{key.replace('_', ' ')} {value}"
                for key, value in found[stage].items()
            ),
        )
        for stage, label in STAGES.items()
    ]
    parts = [_table(("Stage", "Outcome"), rows)]
    if found["deferred"]:
        parts += [
            "<h3>Deferred, discarded or waiting</h3>",
            _table(
                ("Stage", "What", "Reason"),
                (
                    (STAGES[d["stage"]], d["subject"], d["reason"])
                    for d in found["deferred"]
                ),
            ),
        ]
    return "\n".join(parts)


def _counts(found: dict) -> str:
    return ", ".join(f"{name} {count}" for name, count in found.items())


def _weight_part(part: dict) -> str:
    if part["tokens"] is None:
        return "unavailable"
    approx = "" if part["label"] == "measured" else "≈"
    return f"{approx}{part['tokens']:,} tokens, {part['label']} from {part['from']}"


def inventory(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """The tooling inventory and context weight as the run ended (8.1, 8.2,
    10.4): MCP servers configured against their use per harness, the needs a
    tool could have met, and the context before the first prompt with its
    largest contributors, flagged over the configured share of a known window."""
    found = tooling.load(conn, run["id"])
    if found is None:
        return _empty("The tooling inventory and context weight are not measured yet.")
    parts = ["<h3>MCP servers and tools by harness</h3>"]
    for name, h in found["harnesses"].items():
        servers = sorted(set(h["configured"]) | set(h["servers"]))
        rows = [
            (
                server,
                "configured" if server in h["configured"] else "not configured",
                sum(h["servers"].get(server, {}).values()),
                _counts(h["servers"].get(server, {})),
            )
            for server in servers
        ]
        notes = [f"{_plural(h['sessions'], 'session')} read"]
        if h["never_used"]:
            notes.append(f"Never used: {', '.join(h['never_used'])}")
        if h["available"]:
            notes.append(
                "In the shared definitions, not configured: "
                + ", ".join(h["available"])
            )
        if h["tools"]:
            notes.append(f"Other tools used: {_counts(h['tools'])}")
        if h["skills"]:
            notes.append(f"Skills used: {_counts(h['skills'])}")
        parts.append(f"<h4>{escape(name)}</h4>")
        if rows:
            parts.append(_table(("Server", "State", "Uses", "Tools used"), rows))
        parts.append(
            "<ul>" + "".join(f"<li>{escape(note)}</li>" for note in notes) + "</ul>"
        )
    if found["needs"]:
        parts += [
            "<h3>Needs a configured or available tool could have met</h3>",
            _table(
                ("Session", "Observed", "Pattern"),
                ((n["session"], n["quote"], n["key"]) for n in found["needs"]),
            ),
        ]
    share = found["share"]
    rows = []
    for row in found["context"]:
        total = row["total"]
        shown = (
            "unavailable"
            if total["tokens"] is None
            else f"{total['tokens']:,} tokens, {total['label']}"
        )
        if row["window"]:
            shown += f" of a {row['window']:,}-token window"
        cell = escape(shown)
        if row["flagged"]:
            cell += " " + pill(f"over {share:.0%} of the window", "warning")
        rows.append(
            (
                row["harness"],
                row["model"] or "unknown",
                _weight_part(row["instructions"]),
                _weight_part(row["skills"]),
                _weight_part(row["tool_schemas"]),
                _weight_part(row["measured"]),
                Html(cell),
                ", ".join(f"{c['name']} ≈{c['tokens']:,}" for c in row["contributors"]),
            )
        )
    parts += [
        "<h3>Context before the first prompt</h3>",
        '<p class="muted">Estimates are UTF-8 bytes divided by four; a measured '
        "value comes from a session's own usage record.</p>",
        _table(
            (
                "Harness",
                "Model",
                "Instructions",
                "Skill listings",
                "Tool schemas",
                "Measured",
                "Total",
                "Largest contributors",
            ),
            rows,
        ),
    ]
    if found["problems"]:
        items = "".join(f"<li>{escape(p)}</li>" for p in found["problems"])
        parts.append(
            '<div class="callout callout-warning">'
            f"<strong>Could not be read, skipped.</strong><ul>{items}</ul></div>"
        )
    return "\n".join(parts)


def unmerged(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Branches from earlier dreams that still hold unmerged changes (9.14, 10.4)."""
    rows = conn.execute(
        "SELECT c.repo, c.branch, c.run, c.summary, c.status FROM changes c"
        " JOIN runs r ON r.id = c.run"
        " WHERE c.run != ? AND r.started_at <= ? AND c.branch IS NOT NULL"
        f" AND c.status IN {UNMERGED} ORDER BY c.repo, c.branch, c.created_at, c.id",
        (run["id"], run["started_at"]),
    ).fetchall()
    if not rows:
        return _empty("No branch from an earlier dream is waiting to be merged.")
    groups = groupby(rows, key=lambda r: (r["repo"], r["branch"], r["run"]))
    return _table(
        ("Repository", "Branch", "Dream", "Changes"),
        (
            (
                repo,
                Html(f"<code>{escape(branch)}</code>"),
                dream,
                Html(
                    "<br>".join(
                        f"{escape(c['summary'])} ({escape(c['status'])})" for c in group
                    )
                ),
            )
            for (repo, branch, dream), group in groups
        ),
    )


def config_changes(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """The dream's changes to checks, restrictions or tool lists (10.4), and to
    dreaming's own instructions, which apply from the next dream (7.3, 7.4)."""
    rows = []
    for change in conn.execute(
        "SELECT repo, summary, status, edits FROM changes WHERE run = ?"
        " ORDER BY repo, created_at, id",
        (run["id"],),
    ):
        paths = sorted({edit["path"] for edit in json.loads(change["edits"] or "[]")})
        touched = {
            kind: [p for p in paths if any(fnmatchcase(p, pat) for pat in patterns)]
            for kind, patterns in CONFIG_PATHS.items()
        }
        touched = {kind: found for kind, found in touched.items() if found}
        if not touched:
            continue
        shown = sorted({p for found in touched.values() for p in found})
        rows.append(
            (
                change["repo"],
                change["summary"],
                ", ".join(touched),
                Html("<br>".join(f"<code>{escape(p)}</code>" for p in shown)),
                change["status"],
            )
        )
    if not rows:
        return _empty(
            "No change in this dream touches checks, restrictions, tool lists or "
            "dreaming's instructions."
        )
    note = (
        '<p class="muted">A change to dreaming\'s own instructions or configuration '
        "applies from the next dream once merged; this dream kept the "
        "configuration it started with.</p>"
    )
    return note + _table(("Repository", "Change", "Touches", "Paths", "Status"), rows)


def unavailable_integrations(
    conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict
) -> list[tuple[str, str]]:
    """Optional integrations the dream went on without (14.4): backends whose
    latest probe found them unusable (5.5), knowledge folders that do not exist
    (4.6) and MCP tools the shared definitions do not supply."""
    probes: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT backend, outcome, error FROM calls WHERE run = ? AND role = 'probe'"
        " ORDER BY rowid",
        (run["id"],),
    ):
        probes[row["backend"]] = row
    found = [
        (f"backend {name}", row["error"] or "not usable")
        for name, row in sorted(probes.items())
        if row["outcome"] == "unavailable"
    ]
    found += [
        (f"knowledge folder {folder}", "does not exist, so research went without it")
        for folder in cfg["knowledge_folders"]
        if not Path(folder).expanduser().is_dir()
    ]
    found += [
        (f"{m['tool']} for the {m['role']}", m["reason"])
        for m in mcp.unavailable_tools(cfg)
    ]
    return found


def _warning(warning: dict) -> tuple[str, str, str]:
    text = warning.get("message") or warning.get("text") or json.dumps(warning)
    kind = warning.get("code") or warning.get("role") or ""
    return warning.get("source", ""), kind, text


def sources(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """What the dream went without or could not attribute: unavailable optional
    integrations (14.4), sources and projects processed under the unassigned
    domain (2.3), origins counted as interactive because nothing maps them
    (2.9), and listing and unattributed server warnings (2.4)."""
    parts = []
    missing = unavailable_integrations(conn, run, cfg)
    if missing:
        parts += [
            "<h3>Unavailable integrations</h3>",
            '<p class="muted">The dream went on without them.</p>',
            _table(("Integration", "Why"), missing),
        ]
    notes = json.loads(run["selection_notes"] or "{}")
    if notes.get("unmapped_domains"):
        parts += [
            "<h3>Under the unassigned domain</h3>",
            '<p class="muted">No configured domain maps these sources and projects, '
            "so their sessions were processed under the unassigned domain.</p>",
            _table(("Source", "Project"), notes["unmapped_domains"]),
        ]
    if notes.get("unmapped_origins"):
        parts += [
            "<h3>Unmapped client origins</h3>",
            '<p class="muted">Counted as interactive because no configured origin '
            "maps them.</p>",
            _table(("Source", "Origin"), notes["unmapped_origins"]),
        ]
    warnings = [*notes.get("warnings", []), *notes.get("server_warnings", [])]
    if warnings:
        parts += [
            "<h3>Session source warnings</h3>",
            '<p class="muted">Listing warnings, and server warnings and errors no '
            "request is attributed to; none is attached to a session.</p>",
            _table(("Source", "Kind", "Warning"), map(_warning, warnings)),
        ]
    return "\n".join(parts)


def forgotten(conn: sqlite3.Connection, run: sqlite3.Row, cfg: dict) -> str:
    """Forgotten sessions, which no dream reads or counts until restored (2.11)."""
    rows = conn.execute(
        "SELECT source, session_id FROM sessions_seen WHERE forgotten = 1"
        " ORDER BY source, session_id"
    ).fetchall()
    if not rows:
        return ""
    items = "".join(
        f"<li><code>{escape(r['source'])}:{escape(r['session_id'])}</code></li>"
        for r in rows
    )
    return (
        '<p class="muted">Forgotten sessions are left out of every dream, count and '
        "replay until restored with <code>dream restore SESSION</code>; their "
        f"stored data is kept.</p><ul>{items}</ul>"
    )


# The page's sections in order: anchor, heading and body. A body returning "" is
# left out, with its table-of-contents entry.
SECTIONS: list[tuple[str, str, Body]] = [
    ("statistics", "Statistics", statistics),
    ("sources", "Sources and integrations", sources),
    ("outcomes", "Outcomes by stage", outcomes),
    ("knowledge", "Knowledge retained", knowledge),
    ("learning", "Learning recommendations", learning),
    ("changes", "Repository changes", changes),
    ("candidates", "Candidates", candidates),
    ("research", "Research", research),
    ("currency", "Currency review", currency),
    ("effect", "Effect of applied changes", effect),
    ("inventory", "Tooling inventory and context weight", inventory),
    ("unmerged", "Unmerged branches from earlier dreams", unmerged),
    ("config-changes", "Checks, restrictions and tool lists", config_changes),
    ("forgotten", "Forgotten sessions", forgotten),
]

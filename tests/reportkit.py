"""Store rows and page lookups for report tests."""

import json
import re

from runkit import add_run, fake_config

from dreaming import report, steps
from dreaming.steps import NewStep

RUN = "dream-test"
NOW = "2026-11-15T09:00:00+00:00"


def dream(db, run_id=RUN, *, started_at=NOW, **fields):
    """A run row with the default budget."""
    return add_run(db, run_id, fake_config(), started_at=started_at, **fields)


def page(db, run_id=RUN, cfg=None):
    return report.render(db, run_id, cfg or fake_config())


def section(html, anchor):
    """The body of one section, or None when the page leaves it out."""
    found = re.search(rf'<section id="{anchor}">(.*?)</section>', html, re.S)
    return found.group(1) if found else None


def read_step(db, source, sid, state="done", reason=None, *, parts=1, run_id=RUN):
    """A Read step and its reader calls, one per part."""
    key = f"read:{source}:{sid}"
    ref = json.dumps({"source": source, "id": sid})
    steps.add(db, run_id, [NewStep(key, "read", input_ref=ref)])
    steps.finish(db, run_id, key, state, reason)
    for part in range(1, parts + 1):
        call(db, f"{key}#p{part}", run_id=run_id)


def call(
    db,
    key,
    *,
    backend="claude",
    model="sonnet",
    tokens=(100, 20),
    seconds=2.0,
    estimated=0,
    role="reader",
    run_id=RUN,
):
    db.execute(
        "INSERT INTO calls (run, step_key, attempt, role, backend, model, tokens_in,"
        " tokens_out, usage_estimated, seconds, outcome, started_at, ended_at)"
        " VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?)",
        (run_id, key, role, backend, model, *tokens, estimated, seconds, NOW, NOW),
    )


def observation(db, evidence_id, *, pattern_id=None, run_id=RUN):
    db.execute(
        "INSERT INTO observations (evidence_id, run, source, session_id, entry_id,"
        " location, representation_version, quote, kind, modality, lens,"
        " lens_version, pattern_id)"
        " VALUES (?, ?, 'claude', 's1', ?, ?, 1, 'a quote', 'user_statement',"
        " 'asserted', 'preferences', 1, ?)",
        (evidence_id, run_id, evidence_id, evidence_id, pattern_id),
    )


def rejection(db, what, reason, *, run_id=RUN):
    steps.record_rejection(db, run_id, "read:x", what, None, reason)


def pattern(db, pattern_id, *, run_id=RUN):
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at, created_run)"
        " VALUES (?, ?, 'preferences', ?, ?)",
        (pattern_id, pattern_id, NOW, run_id),
    )


def revision(
    db,
    item_id,
    kind,
    content,
    *,
    status="active",
    version=1,
    reason=None,
    supporting=("o1",),
    contradicting=(),
    scope=None,
    run_id=RUN,
):
    db.execute(
        "INSERT OR IGNORE INTO knowledge_items (id, version, kind, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, version, kind, status, NOW, NOW),
    )
    db.execute(
        "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
        " scope, confidence, confidence_basis, supporting, contradicting,"
        " originating_run, actor, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'medium', 'three sessions', ?, ?, ?, 'dream',"
        " ?, ?)",
        (
            item_id,
            version,
            content,
            kind,
            status,
            json.dumps(scope or {"projects": ["/work/app"]}),
            json.dumps(list(supporting)),
            json.dumps(list(contradicting)),
            run_id,
            reason,
            NOW,
        ),
    )


def finding(db, finding_id, claim, sources, *, question_id=None, run_id=RUN):
    db.execute(
        "INSERT INTO research_findings (id, run, question, finding, sources,"
        " question_id, created_at) VALUES (?, ?, 'q', ?, ?, ?, ?)",
        (finding_id, run_id, claim, json.dumps(sources), question_id, NOW),
    )


def source(title, link, passage="the passage relied on"):
    return {
        "title": title,
        "link": link,
        "retrieved_on": "2026-11-14",
        "passage": passage,
        "kind": "web",
    }


def recommendation(
    db, learning_id, summary, *, evidence=("o1",), research=(), run_id=RUN
):
    db.execute(
        "INSERT INTO learning (id, run, scope, summary, evidence, research,"
        " next_step, outcome, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'read the guide', 'fewer stalls', ?, ?)",
        (
            learning_id,
            run_id,
            json.dumps({"domains": ["coding"]}),
            summary,
            json.dumps(list(evidence)),
            json.dumps(list(research)),
            NOW,
            NOW,
        ),
    )


def change(
    db,
    change_id,
    summary,
    *,
    repo="agentic",
    status="proposed",
    branch=None,
    paths=("rules/style.md",),
    run_id=RUN,
):
    edits = [{"path": path, "section": "S", "action": "add"} for path in paths]
    db.execute(
        "INSERT INTO changes (id, run, repo, branch, status, summary, edits,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (change_id, run_id, repo, branch, status, summary, json.dumps(edits), NOW),
    )


def candidate(
    db,
    candidate_id,
    summary,
    *,
    position=None,
    status="open",
    reason=None,
    signals=None,
    deferred_steps=(),
    run_id=RUN,
):
    db.execute(
        "INSERT INTO candidates (id, run, proposal, repo, summary, position,"
        " signals, status, reason, deferred_steps, created_at)"
        " VALUES (?, ?, 'change', 'agentic', ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate_id,
            run_id,
            summary,
            position,
            json.dumps(signals or {}),
            status,
            reason,
            json.dumps(list(deferred_steps)),
            NOW,
        ),
    )


def currency_finding(db, finding_id, target, claim, sources, *, status, run_id=RUN):
    db.execute(
        "INSERT INTO currency_findings (id, run, target, claim, sources, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (finding_id, run_id, target, claim, json.dumps(sources), status, NOW, NOW),
    )

"""Knowledge rules: promotion, scope, conflicts, revisions, ranking and learning
dispositions (4.5, 11.3, 13.2-13.9, 15.3)."""

import json
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sessiondocs import add_observation, add_pattern, add_run, pending

from dreaming import config, knowledge, store
from dreaming.errors import DreamingError, Rejected

NOW = "2026-11-15T09:00:00+00:00"
PROJECT = "/work/repo"
SCOPE = {
    "domains": [],
    "projects": [PROJECT],
    "workflows": [],
    "global": False,
    "unresolved": False,
}
NO_SCOPE = {
    "domains": [],
    "projects": [],
    "workflows": [],
    "global": False,
    "unresolved": False,
}


@pytest.fixture
def cfg():
    return config.build_config({"thresholds": {"min_sessions": 3}})


@pytest.fixture
def reviser(db, cfg):
    add_run(db, "dream-1")
    add_pattern(db, "p1")
    return knowledge.Reviser(
        db, cfg, now=NOW, run_id="dream-1", backend="fake", model="fake-model"
    )


def evidence(db, eid, session, *, thread=None, **fields):
    """An observation in its own session (or `thread`, shared across copies)."""
    db.execute(
        "INSERT OR IGNORE INTO sessions_seen (source, session_id, thread_id, state)"
        " VALUES ('claude', ?, ?, 'read')",
        (session, thread),
    )
    fields.setdefault("kind", "tool_result")
    fields.setdefault("project", PROJECT)
    fields.setdefault("domain", "software")
    add_observation(db, eid, "claude", session, pattern_id="p1", **fields)
    return eid


def sessions_of(db, count, prefix="e", **fields):
    return [
        evidence(db, f"{prefix}{i}", f"{prefix}-s{i}", **fields) for i in range(count)
    ]


def op(supporting=(), *, kind="preference", content="prefers uv over pip", **fields):
    return {
        "op": "create",
        "item_id": None,
        "base_version": None,
        "kind": kind,
        "content": content,
        "scope": SCOPE,
        "confidence": "low",
        "confidence_basis": "",
        "supporting": list(supporting),
        "contradicting": [],
        "pattern_id": "p1",
        "reason": "seen",
        **fields,
    }


def item(db, item_id):
    return knowledge.current(db, item_id)


def revisions(db, item_id):
    return db.execute(
        "SELECT * FROM knowledge_revisions WHERE item_id = ? ORDER BY version",
        (item_id,),
    ).fetchall()


# Promotion (4.5, 13.3, 13.6)


def test_an_inferred_item_is_promoted_at_min_sessions_distinct_sessions(db, reviser):
    two = reviser.apply(op(sessions_of(db, 2)), 0)
    assert item(db, two)["status"] == "candidate"
    three = reviser.apply(op(sessions_of(db, 3, "f"), content="runs make test"), 1)
    assert item(db, three)["status"] == "active"


def test_one_interaction_counts_once_whichever_copies_it_was_read_through(db, reviser):
    ids = [
        evidence(db, "c1", "codex-copy", thread="t1"),
        evidence(db, "c2", "lmstudio-copy", thread="t1"),
        evidence(db, "c3", "other", thread="t2"),
    ]
    assert knowledge.evidence_support(db, ids).sessions == 2
    assert item(db, reviser.apply(op(ids), 0))["status"] == "candidate"


def test_an_explicit_user_statement_applies_at_once_within_its_scope(db, reviser):
    stated = evidence(db, "u1", "s1", kind="user_statement", modality="asserted")
    hedged = evidence(db, "u2", "s2", kind="user_statement", modality="hedged")
    assert item(db, reviser.apply(op([stated]), 0))["status"] == "active"
    created = reviser.apply(op([hedged], content="maybe prefers ruff"), 1)
    assert item(db, created)["status"] == "candidate"


def test_an_approach_worked_claim_needs_a_confirmed_success(db, reviser):
    recurring = sessions_of(db, 4)
    unconfirmed = reviser.apply(op(recurring, kind="workflow"), 0)
    assert item(db, unconfirmed)["status"] == "candidate"
    confirmed = evidence(db, "ok", "s-ok", outcome="check_confirmed")
    worked = reviser.apply(
        op([*recurring, confirmed], kind="workflow", content="tests first works"), 1
    )
    assert item(db, worked)["status"] == "active"


def test_a_confirmed_failure_counts_against_an_approach(db, reviser):
    failed = evidence(db, "bad", "s-bad", outcome="failed")
    success = evidence(db, "ok", "s-ok", outcome="user_confirmed")
    created = reviser.apply(
        op([*sessions_of(db, 3), success, failed], kind="workflow"), 0
    )
    row = item(db, created)
    assert row["status"] == "contested"
    assert json.loads(row["contradicting"]) == ["bad"]
    assert "bad" not in json.loads(row["supporting"])


@given(
    st.permutations(["e0", "e1", "e2", "u1", "f1"]),
    st.sampled_from(["preference", "workflow", "learning"]),
)
def test_promotion_does_not_depend_on_evidence_order(order, kind):
    conn = store.connect(":memory:")
    store.migrate(conn)
    add_pattern(conn, "p1")
    for i in range(3):
        evidence(conn, f"e{i}", f"s{i}")
    evidence(conn, "u1", "s9", kind="user_statement", modality="tentative")
    evidence(conn, "f1", "s8", outcome="failed")
    cfg = config.build_config({})
    first = knowledge.judge(conn, cfg, kind, ["e0", "e1", "e2", "u1", "f1"])
    shuffled = knowledge.judge(conn, cfg, kind, order)
    assert shuffled == first


# Learning recommendations (15.1, 15.3)


def test_learning_rests_on_the_users_own_requests_or_stated_goal(db, reviser):
    asks = [
        evidence(db, f"h{i}", f"h-s{i}", kind="user_statement", modality="tentative")
        for i in range(2)
    ]
    failures = sessions_of(db, 3, "t", kind="tool_result")
    weak = reviser.apply(op([*asks, *failures], kind="learning"), 0)
    assert item(db, weak)["status"] == "candidate"  # tool failures do not count
    third = evidence(db, "h9", "h-s9", kind="user_statement", modality="tentative")
    learning = reviser.apply(
        op([*asks, third], kind="learning", content="learn async rust"), 1
    )
    assert item(db, learning)["status"] == "active"
    goal = evidence(db, "g1", "g-s1", kind="user_statement", modality="asserted")
    stated = reviser.apply(op([goal], kind="learning", content="learn swift"), 2)
    assert item(db, stated)["status"] == "active"


def add_learning(db, learning_id="l1"):
    db.execute(
        "INSERT INTO learning (id, summary, created_at, updated_at)"
        " VALUES (?, 'learn async', ?, ?)",
        (learning_id, NOW, NOW),
    )
    return db.execute("SELECT * FROM learning WHERE id = ?", (learning_id,))


def test_learning_dispositions_are_kept_and_deferrals_end_on_their_date(db):
    add_learning(db)
    today = date(2026, 11, 15)

    def disposition():
        row = db.execute("SELECT * FROM learning WHERE id = 'l1'").fetchone()
        return knowledge.learning_disposition(row, today)

    knowledge.set_learning_disposition(
        db, "l1", "deferred", now=NOW, until="2026-11-20"
    )
    assert disposition() == "deferred"
    knowledge.set_learning_disposition(
        db, "l1", "deferred", now=NOW, until="2026-11-15"
    )
    assert disposition() == "open"
    knowledge.set_learning_disposition(db, "l1", "deferred", now=NOW)
    assert disposition() == "deferred"  # until the user resumes it
    knowledge.set_learning_disposition(db, "l1", "open", now=NOW)
    assert disposition() == "open"
    knowledge.set_learning_disposition(db, "l1", "dismissed", now=NOW, reason="know it")
    row = db.execute("SELECT * FROM learning WHERE id = 'l1'").fetchone()
    assert (row["disposition"], row["reason"]) == ("dismissed", "know it")


@pytest.mark.parametrize(
    "disposition, until, code",
    [
        ("mastered", None, "invalid_request"),
        ("dismissed", "2026-12-01", "invalid_request"),
        ("deferred", "soon", "invalid_request"),
    ],
)
def test_invalid_learning_dispositions_are_refused(db, disposition, until, code):
    add_learning(db)
    with pytest.raises(DreamingError) as excinfo:
        knowledge.set_learning_disposition(db, "l1", disposition, now=NOW, until=until)
    assert excinfo.value.code == code


def test_an_unknown_learning_recommendation_is_not_found(db):
    with pytest.raises(DreamingError) as excinfo:
        knowledge.set_learning_disposition(db, "nope", "addressed", now=NOW)
    assert excinfo.value.code == "not_found"


# Conflicts, supersession and dismissal (13.4, 13.5, 13.9)


def test_conflicting_evidence_in_scope_makes_an_item_contested(db, reviser):
    support = sessions_of(db, 3)
    against = evidence(db, "x1", "x-s1")
    created = reviser.apply(op(support, contradicting=[against]), 0)
    assert item(db, created)["status"] == "contested"
    user = evidence(db, "u1", "u-s1", kind="user_statement", modality="asserted")
    settled = reviser.apply(
        op([user], contradicting=[against], content="prefers uv everywhere here"), 1
    )
    assert item(db, settled)["status"] == "active"


def stated_item(db, reviser):
    user = evidence(db, "u1", "u-s1", kind="user_statement", modality="asserted")
    return reviser.apply(op([user]), 0)


def test_inferred_evidence_never_contests_or_dismisses_a_users_correction(db, reviser):
    created = stated_item(db, reviser)
    inferred = sessions_of(db, 3, "x")
    contest = {**op(contradicting=inferred), "op": "contest", "item_id": created}
    with pytest.raises(Rejected, match="explicit statement"):
        reviser.apply({**contest, "base_version": 1}, 1)
    with pytest.raises(Rejected, match="only the user dismisses"):
        reviser.apply({**contest, "op": "dismiss", "base_version": 1}, 2)
    newer = evidence(db, "u2", "u-s2", kind="user_statement", modality="asserted")
    reviser.apply({**contest, "contradicting": [newer], "base_version": 1}, 3)
    assert item(db, created)["status"] == "contested"


def test_only_a_user_correction_supersedes_and_the_history_stays(db, reviser):
    old = reviser.apply(op(sessions_of(db, 3)), 0)
    replace = {
        **op(sessions_of(db, 3, "y"), content="prefers poetry"),
        "op": "supersede",
        "item_id": old,
        "base_version": 1,
    }
    with pytest.raises(Rejected, match="explicit correction"):
        reviser.apply(replace, 1)
    correction = evidence(db, "u1", "u-s1", kind="user_statement", modality="asserted")
    new = reviser.apply({**replace, "supporting": [correction]}, 2)
    assert new != old
    assert item(db, old)["status"] == "superseded"
    assert item(db, new)["status"] == "active"
    assert json.loads(item(db, new)["scope"]) == SCOPE  # within the old scope
    assert [r["status"] for r in revisions(db, old)] == ["active", "superseded"]
    assert f"superseded by {new}" in revisions(db, old)[-1]["reason"]
    with pytest.raises(Rejected, match="superseded"):
        reviser.apply({**op(), "op": "revise", "item_id": old, "base_version": 2}, 3)


def test_a_dismissed_interpretation_never_comes_back_under_a_new_id(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3)), 0)
    reviser.apply({**op(), "op": "dismiss", "item_id": created, "base_version": 1}, 1)
    assert item(db, created)["status"] == "dismissed"
    again = op(sessions_of(db, 3, "z"), content="  Prefers UV over pip. ")
    with pytest.raises(Rejected, match="dismissed item"):
        reviser.apply(again, 2)
    with pytest.raises(Rejected, match="dismissed"):
        reviser.apply(
            {**op(), "op": "revise", "item_id": created, "base_version": 2}, 3
        )
    # Another scope is another interpretation.
    other = {**SCOPE, "projects": ["/work/other"]}
    assert reviser.apply({**again, "scope": other}, 4) != created


def test_an_equivalent_interpretation_revises_the_existing_item(db, reviser):
    created = reviser.apply(op(sessions_of(db, 2)), 0)
    more = sessions_of(db, 1, "m")
    same = reviser.apply(op(more, content="Prefers  uv over pip."), 1)
    assert same == created
    row = item(db, created)
    assert row["version"] == 2
    assert row["status"] == "active"  # the evidence accumulated to 3 sessions
    assert len(json.loads(row["supporting"])) == 3


def test_stale_versions_are_rejected_and_queued(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3)), 0)
    revise = {**op(), "op": "revise", "item_id": created, "content": "prefers uv"}
    reviser.apply({**revise, "base_version": 1}, 1)
    with pytest.raises(Rejected, match="at version 2, not the 1 read"):
        reviser.apply({**revise, "base_version": 1}, 2)
    assert pending(db)[created] == 1


def test_applying_an_operation_records_the_reassessment_conclusion(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3)), 0)
    knowledge.mark_pending(db, [created])
    reviser.apply(
        {
            **op(),
            "op": "confirm",
            "item_id": created,
            "base_version": 1,
            "reason": "still holds",
        },
        1,
    )
    assert pending(db)[created] == 0
    last = revisions(db, created)[-1]
    assert (last["status"], last["reason"]) == ("active", "still holds")


def test_a_run_rewrites_at_most_its_share_of_a_scope(db, cfg):
    add_run(db, "dream-1")
    add_pattern(db, "p1")
    cfg["thresholds"]["max_revision_fraction"] = 0.4
    setup = knowledge.Reviser(db, cfg, now=NOW, run_id="dream-1")
    support = sessions_of(db, 3)
    items = [setup.apply(op(support, content=f"item {i}"), i) for i in range(5)]
    reviser = knowledge.Reviser(db, cfg, now=NOW, run_id="dream-1")
    dismiss = {**op(), "op": "dismiss", "base_version": 1}
    for item_id in items[:2]:
        reviser.apply({**dismiss, "item_id": item_id}, 0)
    with pytest.raises(Rejected, match="2 of 5 active items"):
        reviser.apply({**dismiss, "item_id": items[2]}, 0)
    assert pending(db)[items[2]] == 1
    # Another scope has its own allowance, of at least one.
    elsewhere = {**SCOPE, "projects": ["/work/other"]}
    lone = setup.apply(op(support, content="lone", scope=elsewhere), 9)
    reviser = knowledge.Reviser(db, cfg, now=NOW, run_id="dream-1")
    reviser.apply({**dismiss, "item_id": lone}, 0)


def test_merging_keeps_every_items_evidence(db, reviser):
    a = reviser.apply(op(sessions_of(db, 2), content="uses uv"), 0)
    b = reviser.apply(op(sessions_of(db, 2, "b"), content="runs uv"), 1)
    reviser.merge([a, b], a, "same thing")
    assert item(db, a)["status"] == "active"
    assert len(json.loads(item(db, a)["supporting"])) == 4
    assert item(db, b)["status"] == "superseded"
    c = reviser.apply(
        op(
            sessions_of(db, 3, "c"),
            content="other",
            scope={**SCOPE, "projects": ["/x"]},
        ),
        2,
    )
    with pytest.raises(Rejected, match="share one scope"):
        reviser.merge([a, c], a, "not the same scope")


def test_revisions_record_actor_base_version_run_and_provenance(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3)), 0)
    reviser.apply(
        {
            **op(),
            "op": "revise",
            "item_id": created,
            "base_version": 1,
            "reason": "why",
        },
        1,
    )
    first, second = revisions(db, created)
    assert (first["actor"], first["base_version"], first["originating_run"]) == (
        "dream",
        None,
        "dream-1",
    )
    assert (second["base_version"], second["reason"]) == (1, "why")
    assert (second["backend"], second["model"]) == ("fake", "fake-model")
    assert (second["lens"], second["lens_version"]) == ("prefs", 1)
    assert second["latest_support_date"] == "2026-11-01"


def test_cited_evidence_must_exist_and_count(db, reviser):
    with pytest.raises(Rejected, match="needs supporting evidence"):
        reviser.apply(op([]), 0)
    with pytest.raises(Rejected, match="unknown, dismissed or from a forgotten"):
        reviser.apply(op(["ev-made-up"]), 1)
    forgotten = evidence(db, "f1", "f-s1")
    db.execute("UPDATE sessions_seen SET forgotten = 1 WHERE session_id = 'f-s1'")
    with pytest.raises(Rejected, match="f1"):
        reviser.apply(op([forgotten]), 2)


def test_confidence_is_capped_by_the_counts(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3), confidence="high"), 0)
    row = item(db, created)
    assert row["confidence"] == "medium"
    assert "capped at medium: 3 sessions" in row["confidence_basis"]
    many = reviser.apply(op(sessions_of(db, 6, "m"), content="x", confidence="high"), 1)
    assert item(db, many)["confidence"] == "high"


# Scope (13.2, 13.4)


def test_a_missing_scope_comes_from_the_evidence_and_is_never_global(db, reviser):
    created = reviser.apply(op(sessions_of(db, 3), scope=NO_SCOPE), 0)
    assert json.loads(item(db, created)["scope"]) == {
        "domains": ["software"],
        "projects": [PROJECT],
        "workflows": [],
        "global": False,
        "unresolved": False,
    }
    unknown = sessions_of(db, 3, "n", project=None, domain=None)
    vague = reviser.apply(op(unknown, scope=NO_SCOPE, content="vague"), 1)
    assert json.loads(item(db, vague)["scope"])["unresolved"] is True


def test_recurrence_in_one_domain_never_makes_an_item_global(db, reviser):
    everywhere = {**NO_SCOPE, "global": True}
    with pytest.raises(Rejected, match="more than one domain"):
        reviser.apply(op(sessions_of(db, 6), scope=everywhere), 0)
    across = [*sessions_of(db, 2, "d"), *sessions_of(db, 2, "w", domain="writing")]
    created = reviser.apply(op(across, scope=everywhere, content="terse"), 1)
    assert json.loads(item(db, created)["scope"])["global"] is True
    with pytest.raises(Rejected, match="names no domains"):
        reviser.apply(op(across, scope={**SCOPE, "global": True}), 2)
    with pytest.raises(Rejected, match="both global and unresolved"):
        reviser.apply(op(across, scope={**everywhere, "unresolved": True}), 3)


@pytest.mark.parametrize(
    "scope, query, applies",
    [
        ({**NO_SCOPE, "global": True}, {}, True),
        ({**NO_SCOPE, "unresolved": True}, {"project": PROJECT}, False),
        (NO_SCOPE, {"project": PROJECT}, False),
        (SCOPE, {"project": PROJECT}, True),
        (SCOPE, {"project": "/work/other"}, False),
        (SCOPE, {"domain": "software"}, False),
        ({**NO_SCOPE, "domains": ["software"]}, {"project": PROJECT}, True),
        ({**NO_SCOPE, "domains": ["software"]}, {"domain": "writing"}, False),
        ({**SCOPE, "workflows": ["orbit"]}, {"project": PROJECT}, False),
        (
            {**SCOPE, "workflows": ["orbit"]},
            {"project": PROJECT, "workflow": "orbit"},
            True,
        ),
    ],
)
def test_an_item_applies_when_the_query_supplies_every_dimension_it_constrains(
    scope, query, applies
):
    cfg = config.build_config({"domains": {"projects": {"/work": "software"}}})
    assert knowledge.scope_applies(scope, cfg, **query) is applies


retrievable = st.lists(
    st.fixed_dictionaries(
        {
            "scope": st.sampled_from(
                [
                    {**NO_SCOPE, "global": True},
                    {**NO_SCOPE, "domains": ["software"]},
                    SCOPE,
                    {**SCOPE, "workflows": ["orbit"]},
                ]
            ),
            "confidence": st.sampled_from(knowledge.CONFIDENCE),
            "latest_support_date": st.sampled_from([None, "2026-10-01", "2026-11-01"]),
        }
    ),
    max_size=12,
)


@given(retrievable, st.randoms())
def test_retrieval_order_does_not_depend_on_input_order(found, rng):
    items = [{**entry, "id": f"k-{i}"} for i, entry in enumerate(found)]
    shuffled = list(items)
    rng.shuffle(shuffled)
    assert sorted(shuffled, key=knowledge.retrieval_key) == sorted(
        items, key=knowledge.retrieval_key
    )


def test_retrieval_puts_the_most_specific_and_confident_first():
    items = [
        {
            "id": "k-g",
            "scope": {**NO_SCOPE, "global": True},
            "confidence": "high",
            "latest_support_date": None,
        },
        {"id": "k-p", "scope": SCOPE, "confidence": "low", "latest_support_date": None},
        {
            "id": "k-w",
            "scope": {**SCOPE, "workflows": ["o"]},
            "confidence": "low",
            "latest_support_date": None,
        },
        {
            "id": "k-p2",
            "scope": SCOPE,
            "confidence": "high",
            "latest_support_date": "2026-10-01",
        },
        {
            "id": "k-p3",
            "scope": SCOPE,
            "confidence": "high",
            "latest_support_date": "2026-11-01",
        },
    ]
    ordered = [i["id"] for i in sorted(items, key=knowledge.retrieval_key)]
    assert ordered == ["k-w", "k-p3", "k-p2", "k-p", "k-g"]


# Ranking (11.3)


def test_ranking_decays_by_half_life_and_breaks_ties_by_id(cfg):
    today = date(2026, 11, 15)
    signals = {
        "p-b": knowledge.Signals(3, 2, 1, "2026-11-15"),
        "p-a": knowledge.Signals(3, 2, 1, "2026-11-15"),
        "p-old": knowledge.Signals(9, 2, 1, "2026-10-16"),  # one half-life ago
        "p-many": knowledge.Signals(9, 4, 1, "2026-01-01"),
    }
    ranked = knowledge.rank_patterns(signals, cfg, today)
    assert [pid for pid, _ in ranked] == ["p-many", "p-a", "p-b", "p-old"]
    scores = dict(ranked)
    assert scores["p-a"] == pytest.approx(1.0 * 2 + 1.0 + 0.5)
    assert scores["p-old"] == pytest.approx(1.0 * 2 + 0.5 + 0.5)
    assert knowledge.recency(signals["p-old"], cfg, today) == pytest.approx(0.5)


@given(st.randoms())
def test_ranking_does_not_depend_on_input_order(rng):
    cfg = config.build_config({})
    signals = {
        f"p-{i}": knowledge.Signals(i % 3 + 1, i % 4, i % 2, f"2026-11-{1 + i % 9:02d}")
        for i in range(12)
    }
    items = list(signals.items())
    rng.shuffle(items)
    today = date(2026, 11, 15)
    assert knowledge.rank_patterns(dict(items), cfg, today) == knowledge.rank_patterns(
        signals, cfg, today
    )

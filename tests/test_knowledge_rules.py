import json
from dataclasses import replace
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st
from test_reader import env as reader_fixture

from dreaming.knowledge import (
    KnowledgeRules,
    learning_for_use,
    rank_patterns,
    set_learning_disposition,
)

reader_env = reader_fixture


def seed(store, count=3, *, kind="assistant_suggestion", topic="workflow", outcome="unknown"):
    conn = store.connection
    conn.execute("INSERT OR IGNORE INTO patterns(id,key) VALUES ('p','workflow')")
    for i in range(count):
        conn.execute(
            "INSERT INTO sessions_seen(source,session_id,thread_id,interactive,project) "
            "VALUES ('claude',?,?,1,'/repo')",
            (f"s{i}", f"t{i}"),
        )
        conn.execute(
            "INSERT INTO observations(id,run_id,source,session_id,entry_id,source_location,"
            "representation_version,redacted_quote,kind,modality,outcome,lens_id,lens_version,"
            "pattern_id,backend,project,observed_at,observation_type) "
            "VALUES (?,'r','claude',?,'entry:0','entry:0',1,'Prefer Make',?,'asserted',?,"
            "'preferences','1','p','fake','/repo','2026-11-10T00:00:00Z',?)",
            (f"e{i}", f"s{i}", kind, outcome, topic),
        )
    return [f"e{i}" for i in range(count)]


def item(evidence, **changes):
    result = dict(
        kind="workflow",
        scope={
            "domains": [],
            "projects": [],
            "workflows": [],
            "global": False,
            "unresolved": False,
        },
        summary="Use Make",
        confidence="high",
        confidence_basis="Repeated evidence",
        supporting_evidence=evidence,
        contradicting_evidence=[],
        lens_id="preferences",
    )
    result.update(changes)
    return result


def apply(store, config, operation, **options):
    with store.transaction() as conn:
        return KnowledgeRules(store, config, "r", **options).apply(conn, operation)


def test_promotion_distinct_interactions_scope_and_revisions(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store, 2)
    result = apply(store, config, {"op": "create", "item": item(evidence)})
    assert result["status"] == "candidate"
    store.connection.execute("UPDATE sessions_seen SET thread_id='same'")
    assert apply(store, config, {"op": "create", "item": item(evidence)})["status"] == "candidate"
    store.connection.execute("UPDATE sessions_seen SET thread_id=session_id")
    config = replace(config, thresholds=replace(config.thresholds, min_sessions=2))
    result = apply(store, config, {"op": "create", "item": item(evidence)})
    assert result["status"] == "active"
    row = store.connection.execute("SELECT scope,version FROM knowledge_items").fetchone()
    assert json.loads(row[0])["projects"] == ["/repo"] and row[1] == 2
    assert store.connection.execute("SELECT count(*) FROM knowledge_revisions").fetchone()[0] == 2


def test_explicit_preference_immediate_but_hedged_inferred_and_global_rejected(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store, 1, kind="user_statement", topic="preference")
    assert (
        apply(store, config, {"op": "create", "item": item(evidence, kind="preference")})["status"]
        == "active"
    )
    store.connection.execute("UPDATE observations SET modality='hedged'")
    assert (
        apply(store, config, {"op": "create", "item": item(evidence, summary="Maybe use Make")})[
            "status"
        ]
        == "candidate"
    )
    assert (
        apply(store, config, {"op": "create", "item": item(evidence, scope={"global": True})})[
            "disposition"
        ]
        == "rejected"
    )


def test_worked_requires_success_and_failure_is_counterevidence(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store, 3)
    operation = {"op": "create", "item": item(evidence, kind="approach_worked")}
    assert apply(store, config, operation)["status"] == "candidate"
    store.connection.execute("UPDATE observations SET outcome='failed' WHERE id='e0'")
    result = apply(store, config, operation)
    assert result["status"] != "active"
    store.connection.execute("UPDATE observations SET outcome='check-confirmed' WHERE id='e0'")
    assert apply(store, config, operation)["status"] == "active"


def test_dismissal_equivalence_stale_writes_and_explicit_reactivation(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store)
    created = apply(store, config, {"op": "create", "item": item(evidence)})
    dismiss = dict(
        op="dismiss",
        item_id=created["item_id"],
        base_version=1,
        reason="Wrong",
        evidence_ids=evidence,
    )
    assert apply(store, config, dismiss, actor="user")["status"] == "dismissed"
    assert (
        apply(store, config, {"op": "create", "item": item(evidence, summary=" USE   MAKE ")})[
            "disposition"
        ]
        == "rejected"
    )
    assert (
        apply(
            store, config, {"op": "create", "item": item(evidence, summary="Prefer make targets")}
        )["disposition"]
        == "rejected"
    )
    stale = dict(op="revise", item_id=created["item_id"], base_version=1, item=item(evidence))
    assert apply(store, config, stale)["reason"] == "stale_version"
    stale["base_version"] = 2
    assert apply(store, config, stale)["disposition"] == "rejected"
    assert apply(store, config, stale, actor="user")["status"] == "active"


def test_conflict_correction_and_revision_cap(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store)
    created = apply(store, config, {"op": "create", "item": item(evidence)})
    contested = item(evidence[:2], contradicting_evidence=evidence[2:])
    op = dict(op="revise", item_id=created["item_id"], base_version=1, item=contested)
    assert apply(store, config, op)["status"] == "contested"
    store.connection.execute(
        "UPDATE observations SET kind='user_statement',observation_type='correction'"
    )
    op["base_version"] = 2
    assert apply(store, config, op)["status"] == "active"
    op["base_version"] = 3
    store.connection.execute(
        "UPDATE observations SET kind='assistant_suggestion',observation_type='workflow'"
    )
    assert apply(store, config, op)["disposition"] == "rejected"
    result = apply(
        store,
        config,
        dict(
            op="dismiss",
            item_id=created["item_id"],
            base_version=3,
            reason="Remove",
            evidence_ids=evidence,
        ),
    )
    assert result["disposition"] == "candidate" and result["reason"] == "revision_cap"


def test_learning_dispositions_only_expire_on_date_or_explicit_resume(reader_env):
    store, _, _, _ = reader_env
    store.connection.execute(
        "INSERT INTO learning(id,scope,evidence,next_step,outcome) "
        "VALUES ('l','{}','[]','Read','Understand')"
    )
    set_learning_disposition(store, "l", "deferred_until", until="2026-12-01T00:00:00Z")
    assert (
        store.connection.execute("SELECT disposition FROM learning").fetchone()[0]
        == "deferred_until"
    )
    assert not learning_for_use(store, now=datetime(2026, 11, 30, tzinfo=UTC))
    assert learning_for_use(store, now=datetime(2026, 12, 1, tzinfo=UTC))
    set_learning_disposition(store, "l", "deferred_until")
    assert not learning_for_use(store, now=datetime(2027, 1, 1, tzinfo=UTC))
    set_learning_disposition(store, "l", "open")
    set_learning_disposition(store, "l", "addressed")
    assert store.connection.execute("SELECT disposition FROM learning").fetchone()[0] == "addressed"
    assert not learning_for_use(store)


def test_ranking_decay_tie_break_and_starvation_boost(reader_env):
    store, config, _, _ = reader_env
    seed(store)
    store.connection.execute("INSERT INTO patterns(id,key) VALUES ('q','other')")
    store.connection.execute("UPDATE observations SET pattern_id='q' WHERE id='e0'")
    config = replace(config, thresholds=replace(config.thresholds, w_sessions=0, w_projects=0))
    ranked = rank_patterns(store, config, now=datetime(2026, 12, 10, tzinfo=UTC))
    assert [r["id"] for r in ranked] == ["p", "q"]
    assert ranked[0]["recency"] == 0.5
    for i in range(4):
        store.connection.execute("INSERT INTO runs(id,status) VALUES (?,'complete')", (f"old{i}",))
    store.connection.execute("UPDATE patterns SET last_reviewed_run='old3' WHERE id='p'")
    assert (
        rank_patterns(store, config, now=datetime(2026, 12, 10, tzinfo=UTC), run_id="r")[0]["id"]
        == "q"
    )


def test_revalidation_keeps_conclusions_and_excluded_evidence_cannot_promote(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store)
    created = apply(store, config, {"op": "create", "item": item(evidence)})
    store.connection.execute("UPDATE knowledge_items SET pending_reassessment=1")
    result = apply(
        store,
        config,
        dict(
            op="revalidate",
            item_id=created["item_id"],
            base_version=1,
            evidence_ids=evidence,
            reason="Confirmed against independent evidence",
        ),
    )
    assert result["status"] == "active"
    assert (
        store.connection.execute("SELECT pending_reassessment FROM knowledge_items").fetchone()[0]
        == 0
    )
    apply(
        store,
        config,
        dict(
            op="dismiss",
            item_id=created["item_id"],
            base_version=2,
            evidence_ids=evidence,
            reason="Wrong",
            exclude_evidence=True,
        ),
        actor="user",
    )
    assert not store.connection.execute("SELECT id FROM observations WHERE excluded=0").fetchall()
    assert (
        apply(store, config, {"op": "create", "item": item(evidence)})["reason"]
        == "unavailable_evidence"
    )


def test_late_registration_can_reassess_a_misattributed_correction(reader_env):
    from dreaming.sessions import register_session

    store, config, _, _ = reader_env
    evidence = seed(store, 1, kind="user_statement", topic="correction")
    created = apply(store, config, {"op": "create", "item": item(evidence, kind="preference")})
    register_session(store, source="claude", session_id="s0", tool="automation", run_ref="job")
    revised = dict(
        op="revise",
        item_id=created["item_id"],
        base_version=1,
        item=item(evidence, kind="preference"),
    )
    assert apply(store, config, revised)["status"] == "candidate"
    assert store.connection.execute("SELECT user_resolved FROM knowledge_items").fetchone()[0] == 0


def test_dismissal_cannot_be_bypassed_after_all_original_support_is_excluded(reader_env):
    store, config, _, _ = reader_env
    evidence = seed(store)
    created = apply(store, config, {"op": "create", "item": item(evidence)})
    apply(
        store,
        config,
        dict(
            op="dismiss",
            item_id=created["item_id"],
            base_version=1,
            evidence_ids=evidence,
            reason="Wrong",
            exclude_evidence=True,
        ),
        actor="user",
    )
    # Independent new observations may support the same pattern, but they may
    # not give a dismissed interpretation a new identity.
    store.connection.execute("UPDATE observations SET id=id||'-new',excluded=0")
    proposed = item([e + "-new" for e in evidence], summary="Prefer make targets")
    assert (
        apply(store, config, {"op": "create", "item": proposed})["reason"]
        == "dismissed_interpretation"
    )


@given(st.permutations(["e0", "e1", "e2"]))
def test_promotion_is_independent_of_evidence_order(order):
    from dreaming.knowledge import promotion_status

    rows = [
        dict(
            id=i,
            source="claude",
            session_id=i,
            thread_id=i,
            kind="assistant_suggestion",
            modality="asserted",
            outcome="unknown",
            observation_type="workflow",
        )
        for i in order
    ]
    assert promotion_status(rows, 3, "workflow") == "active"
    assert promotion_status(rows[::-1], 3, "workflow") == "active"

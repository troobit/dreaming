import json
from dataclasses import replace

import pytest
from test_dispatcher import FakeBackend
from test_knowledge_rules import item, seed
from test_reader import env as reader_fixture

from dreaming.backends import Dispatcher
from dreaming.knowledge import forget_session
from dreaming.review import ReviewHandler, review_context
from dreaming.run import Engine
from dreaming.steps import CallResult, Step, Steps

reader_env = reader_fixture


def test_review_retains_knowledge_and_atomically_schedules_followups(reader_env):
    store, config, roles, _ = reader_env
    evidence = seed(store)
    Steps(store, "r").add(Step("review", "Review"))
    operations = [
        dict(op="create", item=item(evidence)),
        dict(
            op="propose_change",
            pattern_id="p",
            evidence_ids=evidence,
            summary="Make target",
            repo=None,
        ),
        dict(op="research", question="What is Make?", evidence_ids=evidence),
        dict(op="revise", item_id="missing", base_version=1, item=item(evidence)),
    ]
    backend = FakeBackend(
        "fake", [CallResult(json.dumps({"operations": operations}), model="review-m")]
    )
    handler = ReviewHandler(store, "r", config, roles)
    engine = Engine(
        store,
        "r",
        {"Review": handler},
        dispatcher=Dispatcher(store, "r", config, {"fake": backend}),
    )
    assert engine.execute() == 5  # Later-phase handlers are intentionally absent.
    assert store.connection.execute("SELECT status,model FROM knowledge_items").fetchone() == (
        "active",
        "review-m",
    )
    steps = Steps(store, "r").list()
    assert {s["phase"] for s in steps} == {"Review", "Research", "Author"}
    author = next(s for s in steps if s["phase"] == "Author")
    research = next(s for s in steps if s["phase"] == "Research")
    assert research["key"] in json.loads(author["depends_on"])
    assert (
        store.connection.execute(
            "SELECT count(*) FROM review_decisions WHERE disposition='rejected'"
        ).fetchone()[0]
        == 1
    )


def test_review_excludes_forgotten_evidence_and_run_records(reader_env):
    store, config, roles, _ = reader_env
    seed(store)
    forget_session(store, "claude", "s0")
    context = review_context(store, "r", config, roles)
    text = json.dumps(context)
    assert '"e0"' not in text and '"e1"' in text
    assert '"entries"' not in text and '"transcript"' not in text


def test_review_context_reserved_clusters_rank_and_omissions(reader_env):
    store, config, roles, _ = reader_env
    seed(store)
    for i in range(40):
        store.connection.execute("INSERT INTO patterns(id,key) VALUES (?,?)", (f"p{i}", f"key{i}"))
        store.connection.execute(
            "INSERT INTO observations SELECT "
            "? ,run_id,source,session_id,entry_id||?,source_location,representation_version,"
            "redacted_quote||?,kind,modality,excluded,outcome,outcome_evidence,lens_id,lens_version,"
            "?,backend,model,subject,project,domain,observed_at,availability,"
            "observation_type,pattern_key,declared_scope,source_entry_id "
            "FROM observations WHERE id='e0'",
            (f"long{i}", str(i), "x" * 1000, f"p{i}"),
        )
    config = replace(
        config, backends={"fake": replace(config.backends["fake"], context_tokens=6000)}
    )
    roles.config = config
    context = review_context(store, "r", config, roles)
    assert context["new_clusters"]
    assert context["omitted_counts"]
    request = roles.build("reviewer", **context)
    assert request.estimated_tokens <= request.context_tokens - request.output_tokens


def test_unmerged_branch_and_old_revert_block_candidates(reader_env):
    store, config, roles, _ = reader_env
    evidence = seed(store)
    store.connection.execute(
        "INSERT INTO changes(id,run_id,pattern_id,repo,branch,status) "
        "VALUES ('old','r','p','/repo','dream/old','waiting')"
    )
    handler = ReviewHandler(store, "r", config, roles)
    output = json.dumps(
        {
            "operations": [
                dict(
                    op="propose_change",
                    pattern_id="p",
                    evidence_ids=evidence,
                    summary="Again",
                    repo="/repo",
                )
            ]
        }
    )
    Steps(store, "r").add(Step("review", "Review"))
    with store.transaction() as conn:
        assert not handler.apply(conn, Step("review", "Review"), output)
    assert (
        "unmerged_branch"
        in store.connection.execute("SELECT reason FROM review_decisions").fetchone()[0]
    )
    store.connection.execute(
        "UPDATE changes SET status='rejected',reverted_at='2026-11-11T00:00:00Z'"
    )
    with store.transaction() as conn:
        assert not handler.apply(conn, Step("review", "Review"), output)


def test_review_transaction_rollback_keeps_knowledge_and_followups_together(reader_env):
    store, config, roles, _ = reader_env
    evidence = seed(store)
    handler = ReviewHandler(store, "r", config, roles)
    Steps(store, "r").add(Step("review", "Review"))
    raw = json.dumps({"operations": [dict(op="create", item=item(evidence))]})
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            handler.apply(conn, Step("review", "Review"), raw)
            raise RuntimeError("crash")
    assert not store.connection.execute("SELECT * FROM knowledge_items").fetchall()
    assert not store.connection.execute("SELECT * FROM review_decisions").fetchall()


def test_current_file_finding_can_be_a_candidate_without_sessions(reader_env):
    store, config, roles, _ = reader_env
    store.connection.execute("INSERT INTO patterns(id,key) VALUES ('p','references:broken')")
    store.connection.execute(
        "INSERT INTO file_findings(id,run_id,repo,revision,path,location,lens_id,"
        "finding,pattern_id,observed_at) VALUES ('f','r','/repo','abc','AGENTS.md','line:4',"
        "'references','Broken link','p','2026-11-10T00:00:00Z')"
    )
    config = replace(config, repositories={"target": {"path": "/repo"}})
    context = review_context(store, "r", config, roles)
    assert context["new_clusters"][0]["evidence"][0]["revision"] == "abc"
    handler = ReviewHandler(store, "r", config, roles)
    raw = json.dumps(
        {
            "operations": [
                dict(
                    op="propose_change",
                    pattern_id="p",
                    evidence_ids=["f"],
                    summary="Fix reference",
                    repo="/repo",
                )
            ]
        }
    )
    Steps(store, "r").add(Step("review", "Review"))
    with store.transaction() as conn:
        followups = handler.apply(conn, Step("review", "Review"), raw)
    assert len(followups) == 1 and followups[0].phase == "Author"


def test_pattern_merge_reuses_identity_and_keeps_history(reader_env):
    store, config, roles, _ = reader_env
    seed(store)
    store.connection.execute("INSERT INTO patterns(id,key) VALUES ('q','related')")
    store.connection.execute("UPDATE observations SET pattern_id='q' WHERE id='e0'")
    handler = ReviewHandler(store, "r", config, roles)
    raw = json.dumps(
        {"operations": [dict(op="merge_patterns", pattern_ids=["p", "q"], target_id="p")]}
    )
    with store.transaction() as conn:
        handler.apply(conn, Step("review", "Review"), raw)
    assert (
        store.connection.execute("SELECT merged_into FROM patterns WHERE id='q'").fetchone()[0]
        == "p"
    )
    assert (
        store.connection.execute("SELECT count(DISTINCT pattern_id) FROM observations").fetchone()[
            0
        ]
        == 1
    )


def test_review_order_is_independent_of_operation_order(reader_env):
    store, config, roles, _ = reader_env
    evidence = seed(store)
    handler = ReviewHandler(store, "r", config, roles)
    operations = [
        dict(op="create", item=item(evidence)),
        dict(op="research", question="How?", evidence_ids=evidence),
    ]
    outputs = []
    for ordered in (operations, operations[::-1]):
        store.connection.execute("BEGIN")
        try:
            followups = handler.apply(
                store.connection, Step("review", "Review"), json.dumps({"operations": ordered})
            )
            outputs.append(
                (
                    store.connection.execute(
                        "SELECT id,version,status,confidence FROM knowledge_items"
                    ).fetchall(),
                    followups,
                )
            )
        finally:
            store.connection.rollback()
    assert outputs[0] == outputs[1]

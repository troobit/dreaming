"""Knowledge operations through `dream api`: recall, search, get, feedback,
export and import (11.7, 13.5, 14.1-14.3, 14.5-14.7, 15.3)."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from runkit import add_run
from sessiondocs import add_observation

from dreaming import api, cli, config, exchange, knowledge, run, store
from dreaming.errors import DreamingError, Rejected

NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
STAMP = "2026-11-15T09:00:00+00:00"
PROJECT = "/work/repo"


def scope(**fields):
    return {
        "domains": [],
        "projects": [],
        "workflows": [],
        "global": False,
        "unresolved": False,
        **fields,
    }


HERE = scope(projects=[PROJECT])


def make_config():
    return config.build_config(
        {
            "thresholds": {"min_sessions": 3, "quote_chars": 40},
            "domains": {"projects": {"/work": "software"}},
        }
    )


@pytest.fixture
def cfg():
    return make_config()


def memory_store():
    conn = store.connect(Path(":memory:"))
    store.migrate(conn)
    return conn


def call(conn, cfg, op, **fields):
    return api.handle(conn, cfg, {"dreaming_api": 1, "op": op, **fields}, now=NOW)


def recall(conn, cfg, where=None, **fields):
    response = call(
        conn, cfg, "knowledge.recall", scope=where or {"project": PROJECT}, **fields
    )
    assert response["ok"], response
    return response


def ids(response):
    return [found["id"] for found in response["items"]]


# Seeding the store


def evidence(conn, eid, session=None, **fields):
    """An observation in its own session; by default an asserted user statement,
    which supports an item on its own (13.3)."""
    session = session or f"s-{eid}"
    conn.execute(
        "INSERT OR IGNORE INTO sessions_seen (source, session_id, state)"
        " VALUES ('claude', ?, 'read')",
        (session,),
    )
    fields.setdefault("project", PROJECT)
    fields.setdefault("domain", "software")
    add_observation(conn, eid, "claude", session, **fields)
    return eid


def revision(
    conn, item_id, version, content, *, run_id=None, status="active", **fields
):
    conn.execute(
        "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
        " scope, confidence, confidence_basis, supporting, contradicting,"
        " latest_support_date, originating_run, actor, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dream', ?)",
        (
            item_id,
            version,
            content,
            fields.get("kind", "preference"),
            status,
            json.dumps(fields.get("scope", HERE)),
            fields.get("confidence", "medium"),
            fields.get("basis", "seen in sessions"),
            json.dumps(list(fields.get("supporting", ()))),
            json.dumps(list(fields.get("contradicting", ()))),
            fields.get("latest", "2026-11-10"),
            run_id,
            STAMP,
        ),
    )
    conn.execute(
        "UPDATE knowledge_items SET version = ?, status = ?, summary = ? WHERE id = ?",
        (version, status, content, item_id),
    )


def item(conn, item_id, content, *, version=1, pending=0, **fields):
    conn.execute(
        "INSERT INTO knowledge_items (id, version, kind, status, scope, summary,"
        " pending_reassessment, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            version,
            fields.get("kind", "preference"),
            fields.get("status", "active"),
            json.dumps(fields.get("scope", HERE)),
            content,
            pending,
            STAMP,
            STAMP,
        ),
    )
    revision(conn, item_id, version, content, **fields)
    return item_id


def supported(conn, item_id, content, **fields):
    """An item resting on one explicit user statement of its own."""
    return item(
        conn, item_id, content, supporting=[evidence(conn, f"e-{item_id}")], **fields
    )


def learning(conn, learning_id, evidence_ids, *, research=(), **fields):
    conn.execute(
        "INSERT INTO learning (id, scope, summary, evidence, research, next_step,"
        " outcome, disposition, deferred_until, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'read the uv guide', 'fewer lock-file questions',"
        " ?, ?, ?, ?)",
        (
            learning_id,
            json.dumps(fields.get("scope", HERE)),
            f"learn {learning_id}",
            json.dumps(list(evidence_ids)),
            json.dumps(list(research)),
            fields.get("disposition", "open"),
            fields.get("until"),
            STAMP,
            STAMP,
        ),
    )


def finding(conn, finding_id="rf-1"):
    conn.execute(
        "INSERT INTO research_findings (id, question, finding, sources, fetched,"
        " created_at) VALUES (?, 'which guide?', 'the uv guide covers projects', ?,"
        " 1, ?)",
        (
            finding_id,
            json.dumps(
                [
                    {
                        "title": "uv",
                        "link": "https://docs.astral.sh/uv/",
                        "retrieved_on": "2026-11-15",
                        "passage": "uv is fast",
                        "kind": "web",
                    }
                ]
            ),
            STAMP,
        ),
    )
    return finding_id


# The envelope (14.1)


def test_every_response_carries_the_envelope(db, cfg):
    supported(db, "k-1", "prefers uv")
    response = recall(db, cfg)
    assert (response["dreaming_api"], response["op"], response["ok"]) == (
        1,
        "knowledge.recall",
        True,
    )


@pytest.mark.parametrize(
    "request_, code",
    [
        ({"op": "knowledge.recall"}, "invalid_request"),
        ({"dreaming_api": 2, "op": "knowledge.recall"}, "incompatible_version"),
        ({"dreaming_api": 1, "op": "knowledge.teleport"}, "unknown_op"),
        ({"dreaming_api": 1}, "invalid_request"),
        (["knowledge.recall"], "invalid_request"),
    ],
)
def test_requests_outside_the_contract_get_stable_codes(db, cfg, request_, code):
    response = api.handle(db, cfg, request_, now=NOW)
    assert (response["dreaming_api"], response["ok"], response["code"]) == (
        1,
        False,
        code,
    )
    assert response["error"]


def test_dream_api_reads_one_request_and_writes_one_response(home, monkeypatch, capsys):
    request = {
        "dreaming_api": 1,
        "op": "knowledge.search",
        "query": "uv",
        "scope": {"project": PROJECT},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert cli.main(["api"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert (response["ok"], response["items"]) == (True, [])

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["api"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert (response["ok"], response["code"]) == (False, "invalid_request")


# Recall (14.2)


def test_recall_returns_applicable_active_items_by_default(db, cfg):
    supported(db, "k-project", "prefers uv")
    supported(db, "k-domain", "writes tests first", scope=scope(domains=["software"]))
    supported(db, "k-global", "writes British English", scope=scope(**{"global": True}))
    supported(db, "k-other", "uses poetry", scope=scope(projects=["/elsewhere"]))
    supported(db, "k-flow", "tags releases", scope=scope(workflows=["release"]))
    supported(db, "k-unresolved", "likes short answers", scope=scope(unresolved=True))
    for status in ("candidate", "contested", "superseded", "dismissed"):
        supported(db, f"k-{status}", f"a {status} item", status=status)
    response = recall(db, cfg)
    assert ids(response) == ["k-project", "k-domain", "k-global"]
    assert response["estimator"] == "utf8_bytes/4"
    assert response["omitted_count"] == 0
    assert 0 < response["estimated_tokens"] <= response["limit"]

    wider = recall(
        db,
        cfg,
        include=["candidate", "dismissed", "unresolved", "out_of_scope"],
        limit=10_000,
    )
    assert wider["omitted_count"] == 0
    assert set(ids(wider)) == {
        "k-project",
        "k-domain",
        "k-global",
        "k-other",
        "k-flow",
        "k-unresolved",
        "k-candidate",
        "k-dismissed",
    }
    assert (
        ids(recall(db, cfg, {"project": PROJECT, "workflow": "release"}))[0] == "k-flow"
    )


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"scope": {}}, "invalid_scope"),
        ({"scope": {"team": "core"}}, "invalid_scope"),
        ({"scope": {"project": 3}}, "invalid_scope"),
        ({"scope": {"project": ""}}, "invalid_scope"),
        ({"scope": {"project": PROJECT}, "include": ["everything"]}, "invalid_request"),
        ({"scope": {"project": PROJECT}, "limit": 0}, "invalid_request"),
        ({"scope": {"project": PROJECT}, "limit": "ten"}, "invalid_request"),
    ],
)
def test_recall_refuses_a_missing_scope_or_bad_options(db, cfg, fields, code):
    response = call(db, cfg, "knowledge.recall", **fields)
    assert (response["ok"], response["code"]) == (False, code)


def test_recalled_items_carry_revision_scope_evidence_and_confidence_basis(db, cfg):
    hedged = evidence(db, "e1", modality="hedged")
    asserted = evidence(db, "e2")
    gone = evidence(db, "e3", "s-gone")
    against = evidence(db, "c1", kind="tool_result")
    item(
        db,
        "k-1",
        "may prefer uv",
        supporting=[hedged, asserted, gone],
        contradicting=[against],
        confidence="low",
        basis="one hedged remark",
    )
    db.execute(
        "UPDATE observations SET quote = ? WHERE evidence_id = 'e1'", ("x" * 99,)
    )
    knowledge.forget_session(db, "s-gone")
    (found,) = recall(db, cfg)["items"]
    assert {key: found[key] for key in found if key != "evidence"} == {
        "type": "knowledge",
        "id": "k-1",
        "revision": 1,
        "kind": "preference",
        "status": "active",
        "content": "may prefer uv",
        "scope": HERE,
        "confidence": "low",
        "confidence_basis": "one hedged remark",
        "latest_support_date": "2026-11-10",
        "pending_reassessment": True,
        "imported": False,
        "withheld": False,
    }
    supporting = found["evidence"]["supporting"]
    # The forgotten session's evidence is left out (2.11).
    assert [ref["id"] for ref in supporting] == ["e1", "e2"]
    assert supporting[0]["quote"] == "x" * 40 + "…"
    assert supporting[0]["modality"] == "hedged"
    assert [ref["id"] for ref in found["evidence"]["contradicting"]] == ["c1"]


def test_a_running_dreams_revisions_are_not_yet_knowledge(db, cfg):
    add_run(db, "dream-1", cfg, status="complete", started_at="2026-11-01T00:00:00")
    add_run(db, "dream-2", cfg, status="running", started_at="2026-11-02T00:00:00")
    supported(db, "k-1", "prefers uv", run_id="dream-1")
    revision(
        db, "k-1", 2, "prefers uv over pip", run_id="dream-2", supporting=["e-k-1"]
    )
    supported(db, "k-new", "runs make check", run_id="dream-2")
    (found,) = recall(db, cfg)["items"]
    assert (found["id"], found["revision"], found["content"]) == (
        "k-1",
        1,
        "prefers uv",
    )
    assert call(db, cfg, "knowledge.get", id="k-new")["code"] == "not_found"

    db.execute("UPDATE runs SET status = 'complete' WHERE id = 'dream-2'")
    found = recall(db, cfg)["items"]
    assert [(f["id"], f["revision"]) for f in found] == [("k-1", 2), ("k-new", 1)]


def test_items_left_without_support_are_withheld_until_asked_for(db, cfg):
    tools = [evidence(db, f"e{i}", kind="tool_result") for i in range(3)]
    item(db, "k-1", "runs make check", supporting=tools)
    assert ids(recall(db, cfg)) == ["k-1"]
    knowledge.forget_session(db, "s-e0")
    assert ids(recall(db, cfg)) == []
    (found,) = recall(db, cfg, include=["withheld"])["items"]
    assert found["withheld"] is True


def test_learning_recommendations_are_told_apart_and_follow_their_disposition(db, cfg):
    asks = [evidence(db, "h1", lens="help-requests")]
    finding(db)
    learning(db, "l-open", asks, research=["rf-1"])
    learning(db, "l-later", asks, disposition="deferred", until="2026-12-01")
    learning(db, "l-ended", asks, disposition="deferred", until="2026-11-01")
    learning(db, "l-done", asks, disposition="addressed")
    learning(db, "l-no", asks, disposition="dismissed")
    response = recall(db, cfg)
    assert sorted(ids(response)) == ["l-ended", "l-open"]
    found = next(f for f in response["items"] if f["id"] == "l-open")
    assert (found["type"], found["status"], found["next_step"]) == (
        "learning_recommendation",
        "open",
        "read the uv guide",
    )
    assert found["helps_achieve"] == "fewer lock-file questions"
    # Research is kept apart from the evidence about the user (15.2).
    assert [ref["id"] for ref in found["evidence"]["supporting"]] == ["h1"]
    assert found["research"] == [
        {
            "id": "rf-1",
            "claim": "the uv guide covers projects",
            "sources": [
                {
                    "title": "uv",
                    "link": "https://docs.astral.sh/uv/",
                    "retrieved_on": "2026-11-15",
                    "kind": "web",
                }
            ],
        }
    ]
    every = recall(db, cfg, include=["deferred", "addressed", "dismissed"])
    assert sorted(ids(every)) == ["l-done", "l-ended", "l-later", "l-no", "l-open"]


SCOPES = [
    HERE,
    scope(projects=["/elsewhere"]),
    scope(domains=["software"]),
    scope(workflows=["release"]),
    scope(**{"global": True}),
    scope(unresolved=True),
]
QUERIES = [
    {"project": PROJECT},
    {"domain": "software"},
    {"workflow": "release"},
    {"project": PROJECT, "workflow": "release"},
]
item_specs = st.tuples(
    st.integers(0, len(SCOPES) - 1),
    st.sampled_from(["active", "active", "candidate", "contested", "dismissed"]),
    st.sampled_from(knowledge.CONFIDENCE),
    st.integers(1, 28),
    st.integers(0, 200),
)


@settings(max_examples=40, deadline=None)
@given(
    st.lists(item_specs, max_size=10),
    st.randoms(use_true_random=False),
    st.integers(1, 600),
    st.sampled_from(QUERIES),
)
def test_recall_stays_within_its_limit_in_an_order_independent_of_input(
    specs, rng, limit, query
):
    cfg = make_config()

    def recalled(order):
        conn = memory_store()
        for index in order:
            where, status, confidence, day, length = specs[index]
            supported(
                conn,
                f"k-{index}",
                f"item {index} " + "x" * length,
                scope=SCOPES[where],
                status=status,
                confidence=confidence,
                latest=f"2026-10-{day:02d}",
            )
        response = call(conn, cfg, "knowledge.recall", scope=query, limit=limit)
        conn.close()
        return response

    order = list(range(len(specs)))
    first = recalled(order)
    rng.shuffle(order)
    assert recalled(order) == first
    assert first["estimated_tokens"] <= limit
    eligible = [
        f"k-{index}"
        for index, (where, status, *_) in enumerate(specs)
        if status == "active" and knowledge.scope_applies(SCOPES[where], cfg, **query)
    ]
    assert set(ids(first)) <= set(eligible)
    assert len(ids(first)) + first["omitted_count"] == len(eligible)
    keys = [knowledge.retrieval_key(found) for found in first["items"]]
    assert keys == sorted(keys)


# Search and get (14.6, 14.7)


def test_search_matches_every_query_term_within_the_default_filtering(db, cfg):
    supported(db, "k-1", "Prefers uv over pip for Python packaging")
    supported(db, "k-2", "Prefers pip")
    supported(db, "k-3", "uv over pip", status="dismissed")
    supported(db, "k-4", "uv over pip", scope=scope(projects=["/elsewhere"]))
    supported(db, "k-5", "uv and pip " + "y" * 60)
    response = call(
        db, cfg, "knowledge.search", query="UV pip", scope={"project": PROJECT}
    )
    assert response["ok"]
    assert ids(response) == ["k-1", "k-5"]
    first, long = response["items"]
    assert (first["revision"], first["summary"]) == (
        1,
        "Prefers uv over pip for Python packaging",
    )
    assert long["summary"] == ("uv and pip " + "y" * 60)[:40] + "…"
    assert "evidence" not in first
    assert response["estimator"] == "utf8_bytes/4"
    blank = call(db, cfg, "knowledge.search", query="  ", scope={"project": PROJECT})
    assert blank["code"] == "invalid_request"


def test_get_returns_the_current_retained_revision_or_not_found(db, cfg):
    supported(db, "k-1", "prefers uv", status="dismissed")
    learning(db, "l-1", ["e-k-1"])
    response = call(db, cfg, "knowledge.get", id="k-1")
    assert response["ok"]
    assert (response["item"]["status"], response["item"]["revision"]) == (
        "dismissed",
        1,
    )
    assert [ref["id"] for ref in response["item"]["evidence"]["supporting"]] == [
        "e-k-1"
    ]
    assert call(db, cfg, "knowledge.get", id="l-1")["item"]["type"] == (
        "learning_recommendation"
    )
    missing = call(db, cfg, "knowledge.get", id="k-missing")
    assert (missing["ok"], missing["code"]) == (False, "not_found")


# Feedback (11.7, 13.5, 14.5, 15.3)


def feedback(conn, cfg, fid="fb-1", **fields):
    request = {
        "feedback_id": fid,
        "revisions": [{"item_id": "k-1", "version": 1, "use": "cited"}],
        "outcome": "success",
        "basis": "user_confirmed",
        **fields,
    }
    return call(conn, cfg, "knowledge.feedback", **request)


def test_feedback_is_kept_with_its_revisions_and_basis_and_queues_reassessment(db, cfg):
    supported(db, "k-1", "prefers uv")
    supported(db, "k-2", "runs make check")
    observations = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    revisions = [
        {"item_id": "k-1", "version": 1, "use": "cited"},
        {"item_id": "k-2", "version": 1, "use": "present_only"},
    ]
    response = feedback(
        db,
        cfg,
        revisions=revisions,
        basis="check_confirmed",
        check="make test",
        run_ref="screw-7",
    )
    assert response["ok"], response
    assert (response["changed"], response["reassess"]) == (True, ["k-1", "k-2"])
    row = db.execute("SELECT * FROM feedback").fetchone()
    assert json.loads(row["revisions"]) == revisions
    assert (row["outcome"], row["basis"], row["check_name"], row["run_ref"]) == (
        "success",
        "check_confirmed",
        "make test",
        "screw-7",
    )
    assert [f["pending_reassessment"] for f in recall(db, cfg)["items"]] == [True, True]
    # Feedback is never evidence about the user (14.5).
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == observations


def test_repeating_a_feedback_id_changes_nothing(db, cfg):
    supported(db, "k-1", "prefers uv")
    feedback(db, cfg)
    db.execute("UPDATE knowledge_items SET pending_reassessment = 0")
    before = db.total_changes
    again = feedback(db, cfg, outcome="failure")
    assert (again["ok"], again["changed"]) == (True, False)
    assert db.total_changes == before
    assert db.execute("SELECT outcome FROM feedback").fetchone()[0] == "success"


@pytest.mark.parametrize(
    "fields, code",
    [
        (
            {"revisions": [{"item_id": "k-x", "version": 1, "use": "cited"}]},
            "not_found",
        ),
        (
            {"revisions": [{"item_id": "k-1", "version": 9, "use": "cited"}]},
            "not_found",
        ),
        (
            {"revisions": [{"item_id": "k-1", "version": 1, "use": "read"}]},
            "invalid_request",
        ),
        ({"basis": "check_confirmed"}, "invalid_request"),
        ({"basis": "vibes"}, "invalid_request"),
        ({"outcome": "great"}, "invalid_request"),
        ({"feedback_id": ""}, "invalid_request"),
        ({"revisions": [], "outcome": None}, "invalid_request"),
        ({"revisions": [], "outcome": "success"}, "invalid_request"),
        ({"correction": {"item_id": "k-1"}}, "invalid_request"),
        ({"learning": {"id": "l-x", "disposition": "addressed"}}, "not_found"),
    ],
)
def test_invalid_feedback_is_refused_and_stores_nothing(db, cfg, fields, code):
    supported(db, "k-1", "prefers uv")
    response = feedback(db, cfg, **fields)
    assert (response["ok"], response["code"]) == (False, code)
    assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def correction(**fields):
    return {
        "revisions": [],
        "outcome": None,
        "correction": {
            "item_id": "k-1",
            "base_version": 1,
            "reason": "said so",
            **fields,
        },
    }


def test_a_users_correction_applies_at_once_and_inferred_evidence_cannot_undo_it(
    db, cfg
):
    tools = [evidence(db, f"e{i}", kind="tool_result") for i in range(1)]
    item(db, "k-1", "prefers uv", supporting=tools, status="candidate")
    response = feedback(db, cfg, **correction(content="prefers uv, except in scripts"))
    assert response["ok"], response
    assert response["revision"] == {"item_id": "k-1", "version": 2}
    row = knowledge.current(db, "k-1")
    assert (row["content"], row["status"], row["actor"], row["feedback_id"]) == (
        "prefers uv, except in scripts",
        "active",
        "user",
        "fb-1",
    )
    (found,) = recall(db, cfg)["items"]
    assert (found["content"], found["withheld"]) == (
        "prefers uv, except in scripts",
        False,
    )

    against = evidence(db, "x1", kind="tool_result")
    reviser = knowledge.Reviser(db, cfg, now=STAMP)
    contest = {
        "op": "contest",
        "item_id": "k-1",
        "base_version": 2,
        "kind": "preference",
        "content": "",
        "scope": None,
        "confidence": "low",
        "confidence_basis": "",
        "supporting": [],
        "contradicting": [against],
        "pattern_id": None,
        "reason": "the tool used pip",
    }
    with pytest.raises(Rejected, match="13.4"):
        reviser.apply(contest, 0)


def test_a_user_may_make_an_item_global(db, cfg):
    supported(db, "k-1", "writes British English")
    response = feedback(db, cfg, **correction(scope=scope(**{"global": True})))
    assert response["ok"], response
    assert ids(recall(db, cfg, {"project": "/somewhere/else"})) == ["k-1"]


def test_a_users_dismissal_can_exclude_the_evidence_it_rested_on(db, cfg):
    tools = [evidence(db, f"e{i}", kind="tool_result") for i in range(3)]
    item(db, "k-1", "prefers uv", supporting=tools)
    item(db, "k-2", "runs make check", supporting=tools[:1])
    response = feedback(db, cfg, **correction(dismiss=True, exclude_evidence=True))
    assert response["ok"], response
    assert response["reassess"] == ["k-2"]
    row = knowledge.current(db, "k-1")
    assert (row["status"], row["actor"]) == ("dismissed", "user")
    excluded = dict(db.execute("SELECT evidence_id, excluded FROM observations"))
    assert excluded == {"e0": 1, "e1": 1, "e2": 1}
    assert "k-1" not in ids(recall(db, cfg, include=["withheld"]))
    assert "k-1" in ids(recall(db, cfg, include=["dismissed"]))


def test_a_correction_to_a_version_that_moved_on_is_refused(db, cfg):
    supported(db, "k-1", "prefers uv", version=2)
    response = feedback(db, cfg, **correction(content="prefers pip"))
    assert (response["ok"], response["code"]) == (False, "stale_version")
    assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert knowledge.current(db, "k-1")["content"] == "prefers uv"


def test_learning_dispositions_are_set_through_feedback(db, cfg):
    learning(db, "l-1", [evidence(db, "h1")])
    response = feedback(
        db,
        cfg,
        revisions=[],
        outcome=None,
        learning={"id": "l-1", "disposition": "deferred", "until": "2026-12-01"},
    )
    assert response["ok"], response
    row = db.execute("SELECT * FROM learning").fetchone()
    assert (row["disposition"], row["deferred_until"]) == ("deferred", "2026-12-01")
    assert ids(recall(db, cfg)) == []


# Export and import (14.3)


def rich_store(conn, cfg):
    add_run(conn, "dream-1", cfg, status="complete", started_at="2026-11-01T00:00:00")
    conn.execute(
        "INSERT INTO patterns (id, key, lens, scope, created_at, created_run)"
        " VALUES ('p-1', 'prefers uv', 'preferences', '{}', ?, 'dream-1')",
        (STAMP,),
    )
    kept = evidence(conn, "e1", pattern_id="p-1")
    forgotten = evidence(conn, "e2", "s-gone", pattern_id="p-1")
    missing = evidence(conn, "e3", pattern_id="p-1")
    dismissed = evidence(conn, "e4", kind="tool_result")
    conn.execute(
        "UPDATE observations SET availability = 'unavailable' WHERE evidence_id = 'e3'"
    )
    conn.execute("UPDATE observations SET excluded = 1 WHERE evidence_id = 'e4'")
    conn.execute(
        "INSERT INTO file_findings (evidence_id, run, repo, revision, path, location,"
        " finding, lens, lens_version, pattern_id) VALUES ('ff1', 'dream-1',"
        " 'agentic', 'abc', 'skills/x.md', 'L3', 'a missing link', 'refs', 1, 'p-1')"
    )
    finding(conn)
    item(
        conn,
        "k-1",
        "prefers uv",
        supporting=[kept, forgotten, missing],
        run_id="dream-1",
    )
    conn.execute("UPDATE knowledge_items SET pattern_id = 'p-1' WHERE id = 'k-1'")
    item(conn, "k-2", "uses pip", supporting=[dismissed, "ff1"], status="dismissed")
    learning(
        conn,
        "l-1",
        [kept],
        research=["rf-1"],
        disposition="deferred",
        until="2026-12-01",
    )
    knowledge.forget_session(conn, "s-gone")
    assert feedback(conn, cfg, **correction(content="prefers uv over pip"))["ok"]


def test_export_then_import_into_a_fresh_store_reproduces_the_export(db, cfg):
    rich_store(db, cfg)
    first = call(db, cfg, "knowledge.export")
    assert first["ok"], first
    document = first["export"]
    assert (document["kind"], document["schema_version"]) == ("dreaming-export", 1)
    marked = {
        o["evidence_id"]: o["forgotten"] for o in document["tables"]["observations"]
    }
    assert marked == {"e1": False, "e2": True, "e3": False, "e4": False}

    fresh = memory_store()
    imported = call(fresh, cfg, "knowledge.import", export=document)
    assert imported["ok"], imported
    assert imported["imported"]["knowledge_items"] == 2
    assert call(fresh, cfg, "knowledge.export")["export"] == document

    before = fresh.total_changes
    again = call(fresh, cfg, "knowledge.import", export=document)
    assert sum(again["imported"].values()) == 0
    assert fresh.total_changes == before

    # Imported items and evidence are marked, and the evidence never counts
    # toward this store's promotion thresholds.
    assert {
        row[0] for row in fresh.execute("SELECT imported FROM knowledge_items")
    } == {1}
    assert {row[0] for row in fresh.execute("SELECT imported FROM observations")} == {1}
    assert knowledge.evidence_support(fresh, ["e1", "e3"]).sessions == 0
    # Forgotten sessions stay excluded, dismissed items stay dismissed, and the
    # imported knowledge is still recalled.
    gone = fresh.execute(
        "SELECT forgotten FROM sessions_seen WHERE session_id = 's-gone'"
    )
    assert gone.fetchone()[0] == 1
    assert knowledge.current(fresh, "k-2")["status"] == "dismissed"
    (found,) = recall(fresh, cfg)["items"]
    assert (found["id"], found["content"], found["imported"]) == (
        "k-1",
        "prefers uv over pip",
        True,
    )
    assert [ref["id"] for ref in found["evidence"]["supporting"]] == ["e1", "e3"]
    assert found["evidence"]["supporting"][1]["availability"] == "unavailable"
    # Imported runs are lineage, not this store's dreams.
    with pytest.raises(DreamingError):
        run.find_run(fresh)


observation_specs = st.tuples(
    st.booleans(),
    st.booleans(),
    st.sampled_from(["available", "unavailable", "changed"]),
    st.sampled_from(["active", "candidate", "dismissed", "superseded"]),
)


@settings(max_examples=25, deadline=None)
@given(st.lists(observation_specs, min_size=1, max_size=6))
def test_importing_an_export_is_faithful_and_idempotent(specs):
    source = memory_store()
    for index, (forgotten, excluded, availability, status) in enumerate(specs):
        eid = evidence(source, f"e{index}")
        source.execute(
            "UPDATE observations SET excluded = ?, availability = ?"
            " WHERE evidence_id = ?",
            (int(excluded), availability, eid),
        )
        source.execute(
            "UPDATE sessions_seen SET forgotten = ? WHERE session_id = ?",
            (int(forgotten), f"s-{eid}"),
        )
        item(source, f"k-{index}", f"item {index}", supporting=[eid], status=status)
    first = exchange.export(source)
    fresh = memory_store()
    exchange.import_(fresh, first)
    assert exchange.export(fresh) == first
    before = fresh.total_changes
    exchange.import_(fresh, first)
    assert fresh.total_changes == before


def test_export_waits_for_an_unfinished_dream(db, cfg):
    add_run(db, "dream-1", cfg, status="interrupted")
    response = call(db, cfg, "knowledge.export")
    assert (response["ok"], response["code"]) == (False, "run_unfinished")
    assert "dream-1" in response["error"]


@pytest.mark.parametrize(
    "document, code",
    [
        (
            {"kind": "dreaming-export", "schema_version": 2, "tables": {}},
            "incompatible_version",
        ),
        (
            {"kind": "something-else", "schema_version": 1, "tables": {}},
            "invalid_request",
        ),
        (
            {"kind": "dreaming-export", "schema_version": 1, "tables": []},
            "invalid_request",
        ),
        (
            {
                "kind": "dreaming-export",
                "schema_version": 1,
                "tables": {"observations": [{"evidence_id": "e1"}]},
            },
            "invalid_export",
        ),
        (None, "invalid_request"),
    ],
)
def test_an_import_outside_the_contract_is_refused_and_changes_nothing(
    db, cfg, document, code
):
    response = call(db, cfg, "knowledge.import", export=document)
    assert (response["ok"], response["code"]) == (False, code)
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

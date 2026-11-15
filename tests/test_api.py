import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dreaming.api import handle_request
from dreaming.config import load_config
from dreaming.knowledge import KnowledgeRules, forget_session
from dreaming.observe import canonical
from dreaming.store import Store


@pytest.fixture
def api_env(tmp_path):
    with Store(tmp_path / "home") as store:
        yield store, load_config(tmp_path / "config.toml")


def request(env, op, **fields):
    return handle_request(*env, {"dreaming_api": 1, "op": "knowledge." + op, **fields})


def seed(store, item_id="k", *, scope=None, status="active", confidence="high", date=None):
    conn = store.connection
    conn.execute("INSERT OR IGNORE INTO runs(id,status) VALUES ('r','complete')")
    conn.execute("INSERT INTO patterns(id,key) VALUES (?,?)", (item_id, item_id))
    conn.execute(
        "INSERT INTO sessions_seen(source,session_id,thread_id,interactive,project) "
        "VALUES ('claude',?,?,1,'/repo')",
        (item_id, item_id),
    )
    conn.execute(
        "INSERT INTO observations(id,run_id,source,session_id,entry_id,source_location,"
        "representation_version,redacted_quote,kind,modality,outcome,lens_id,lens_version,"
        "pattern_id,backend,project,observed_at,observation_type) "
        "VALUES (?,'r','claude',?,'entry:0','entry:0',1,'Maybe use Make [REDACTED]',"
        "'user_statement','asserted','unknown','preferences','1',?,'fake','/repo',?,"
        "'preference')",
        ("e-" + item_id, item_id, item_id, date),
    )
    columns = dict(
        id=item_id,
        kind="preference",
        scope=canonical(scope if scope is not None else {"projects": ["/repo"]}),
        status=status,
        summary="Use Make " + item_id,
        confidence=confidence,
        confidence_basis="User stated preference",
        supporting_evidence=canonical(["e-" + item_id]),
        latest_support_at=date,
        pattern_id=item_id,
        run_id="r",
        actor="dream",
    )
    conn.execute(
        f"INSERT INTO knowledge_items({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(columns.values()),
    )
    columns.pop("pattern_id")
    columns["item_id"] = columns.pop("id")
    columns["version"] = 1
    conn.execute(
        f"INSERT INTO knowledge_revisions({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(columns.values()),
    )


def test_scope_order_estimate_and_evidence(api_env):
    store, config = api_env
    config = replace(config, domains={"projects": {"/repo": "software"}})
    env = store, config
    seed(store, "global", scope={"global": True})
    seed(store, "domain", scope={"domains": ["software"]})
    seed(store, "project", confidence="low")
    seed(store, "workflow", scope={"projects": ["/repo"], "workflows": ["review"]})
    seed(store, "other", scope={"projects": ["/elsewhere"]})
    seed(store, "unresolved", scope={"unresolved": True})
    seed(store, "dismissed", status="dismissed")
    store.connection.execute("UPDATE observations SET modality='hedged'")
    store.connection.execute("UPDATE knowledge_items SET pending_reassessment=1 WHERE id='project'")
    result = request(env, "recall", project="/repo", workflow="review", limit=10000)
    assert result["ok"] and result["estimator"] == "utf8_bytes/4"
    assert [item["id"] for item in result["items"]] == ["workflow", "project", "domain", "global"]
    assert result["items"][1]["pending_reassessment"] is True
    evidence = result["items"][1]["evidence"][0]
    assert evidence["quote"] == "Maybe use Make [REDACTED]" and evidence["modality"] == "hedged"
    assert result["estimated_tokens"] <= 10000
    assert request(env, "recall", project="/repo", limit=0)["items"] == []
    assert (
        request(env, "search", project="/repo", query="MAKE domain")["items"][0]["id"] == "domain"
    )
    assert not request(env, "search", project="/repo", query="nonexistent")["items"]


def test_current_revision_get_and_explicit_nonactive(api_env):
    store, _ = api_env
    seed(store, status="dismissed")
    assert request(api_env, "get", id="k")["item"]["status"] == "dismissed"
    assert request(api_env, "get", id="absent")["code"] == "not_found"
    assert not request(api_env, "recall", project="/repo")["items"]
    assert request(api_env, "recall", project="/repo", include=["dismissed"])["items"]
    assert request(api_env, "recall", scope={"project": 123})["code"] == "invalid_scope"
    assert (
        handle_request(store, api_env[1], {"dreaming_api": True, "op": "knowledge.get"})["code"]
        == "incompatible_version"
    )


def test_forgotten_support_is_withheld_but_history_retained(api_env):
    store, _ = api_env
    seed(store)
    forget_session(store, "claude", "k")
    assert not request(api_env, "recall", project="/repo")["items"]
    current = request(api_env, "get", id="k")["item"]
    assert current["evidence"] == [] and current["pending_reassessment"]


def test_feedback_is_idempotent_and_delivery_is_not_confirmation(api_env):
    store, _ = api_env
    seed(store)
    feedback = dict(
        feedback_id="f1",
        revisions=[{"item_id": "k", "version": 1, "usage": "present_only"}],
        outcome="success",
        basis="check-confirmed",
        check_name="make test",
        run_ref="consumer-run",
    )
    result = request(api_env, "feedback", **feedback)
    assert result["ok"] and result["changed"]
    before = store.connection.total_changes
    assert request(api_env, "feedback", **feedback)["changed"] is False
    assert store.connection.total_changes == before
    assert store.connection.execute("SELECT outcome FROM observations").fetchone()[0] == "unknown"
    row = store.connection.execute("SELECT revisions,basis FROM feedback").fetchone()
    assert json.loads(row[0]) == feedback["revisions"] and row[1] == "check-confirmed"
    assert request(api_env, "get", id="k")["item"]["pending_reassessment"]
    assert not request(
        api_env, "feedback", **{**feedback, "feedback_id": "f2", "check_name": None}
    )["ok"]


def test_feedback_correction_scope_dismissal_history_and_stale_version(api_env):
    store, _ = api_env
    seed(store)
    fields = dict(
        feedback_id="correct",
        revisions=[{"item_id": "k", "version": 1, "usage": "cited"}],
        correction="Use the Makefile targets",
        scope={"domains": ["software"]},
        reason="Applies throughout software work",
        basis="user-confirmed",
    )
    assert request(api_env, "feedback", **fields)["ok"]
    item = request(api_env, "get", id="k")["item"]
    assert item["summary"] == fields["correction"] and item["version"] == 2
    assert item["scope"]["domains"] == ["software"]
    assert not request(api_env, "feedback", **{**fields, "feedback_id": "stale"})["ok"]
    assert request(
        api_env,
        "feedback",
        feedback_id="dismiss",
        revisions=[{"item_id": "k", "version": 2, "usage": "cited"}],
        dismiss=True,
        exclude_evidence=True,
        reason="No longer useful",
        basis="user-confirmed",
    )["ok"]
    assert request(api_env, "get", id="k")["item"]["status"] == "dismissed"
    assert store.connection.execute("SELECT count(*) FROM knowledge_revisions").fetchone()[0] == 3
    assert store.connection.execute("SELECT excluded FROM observations").fetchone()[0] == 1


def test_learning_dispositions_and_currency_notes_are_explicit(api_env):
    store, _ = api_env
    seed(store)
    store.connection.execute(
        "INSERT INTO learning(id,scope,evidence,next_step,outcome,disposition) "
        "VALUES ('learn',?,?,'Read the guide','Understand Make','addressed')",
        (canonical({"projects": ["/repo"]}), canonical(["e-k"])),
    )
    store.connection.execute(
        "INSERT INTO currency_findings(id,target,claim,status) "
        "VALUES ('currency','/repo','Updated guidance','note')"
    )
    assert all(
        i["kind"] != "currency_note" for i in request(api_env, "recall", project="/repo")["items"]
    )
    assert any(
        i["id"] == "learn"
        for i in request(api_env, "recall", project="/repo", include=["addressed"])["items"]
    )
    assert any(
        i["id"] == "currency"
        for i in request(api_env, "recall", project="/repo", include=["currency_notes"])["items"]
    )


def test_export_import_roundtrip_exclusion_history_and_imported_counts(api_env, tmp_path):
    store, config = api_env
    seed(store)
    seed(store, "dismissed", status="dismissed")
    store.connection.execute("UPDATE observations SET availability='changed' WHERE id='e-k'")
    forget_session(store, "claude", "dismissed")
    document = request(api_env, "export")["export"]
    with Store(tmp_path / "imported") as imported:
        env = imported, config
        assert request(env, "import", **{"export": document})["ok"]
        assert request(env, "export")["export"] == document
        before = imported.connection.total_changes
        assert request(env, "import", **{"export": document})["changed"] is False
        assert imported.connection.total_changes == before
        assert (
            imported.connection.execute("SELECT min(imported) FROM knowledge_items").fetchone()[0]
            == 1
        )
        assert (
            imported.connection.execute("SELECT min(imported) FROM observations").fetchone()[0] == 1
        )
        assert request(env, "get", id="dismissed")["item"]["evidence"] == []
        assert request(env, "recall", project="/repo")["items"][0]["id"] == "k"
        operation = {
            "op": "create",
            "item": {
                "kind": "preference",
                "scope": {"projects": ["/repo"]},
                "summary": "Another inferred preference",
                "confidence": "high",
                "confidence_basis": "Imported repeated evidence",
                "supporting_evidence": ["e-k"],
                "contradicting_evidence": [],
                "lens_id": "preferences",
            },
        }
        with imported.transaction() as conn:
            result = KnowledgeRules(imported, config, "r").apply(conn, operation)
        assert result.get("status") != "active"


def test_invalid_export_is_atomic_and_versions_explicit(api_env, tmp_path):
    seed(api_env[0])
    document = request(api_env, "export")["export"]
    document["knowledge_items"][0]["scope"] = {"global": "yes"}
    with Store(tmp_path / "dest") as dest:
        env = dest, api_env[1]
        assert not request(env, "import", **{"export": document})["ok"]
        assert dest.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        document["schema_version"] = 100
        assert request(env, "import", **{"export": document})["code"] == "incompatible_version"


def test_import_scrubs_nested_sources_and_rejects_malformed_references(api_env, tmp_path):
    store, config = api_env
    seed(store)
    token = "sk-" + "a" * 30
    sources = [{"title": "Resource", "url": "https://example.invalid", "text": token}]
    store.connection.execute(
        "INSERT INTO research_findings(id,run_id,question,finding,sources,evidence_ids) "
        "VALUES ('research','r','question','finding',?,?)",
        (canonical(sources), canonical(["e-k"])),
    )
    document = request(api_env, "export")["export"]
    with Store(tmp_path / "imported") as imported:
        assert request((imported, config), "import", **{"export": document})["ok"]
        exported = request((imported, config), "export")["export"]
        assert token not in canonical(exported)
        assert exported["research_findings"][0]["evidence_ids"] == ["e-k"]
    document["research_findings"][0]["sources"] = ["invalid"]
    with Store(tmp_path / "invalid") as imported:
        assert not request((imported, config), "import", **{"export": document})["ok"]
        assert not imported.connection.execute("SELECT * FROM observations").fetchall()


@settings(max_examples=20, deadline=None)
@given(
    st.lists(st.sampled_from(["a", "b", "c", "d"]), unique=True, min_size=1),
    st.integers(min_value=0, max_value=2000),
)
def test_property_limits_and_order_are_independent_of_insertion(ids, limit):
    outputs = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = load_config(root / "config.toml")
        for index, ordered in enumerate((ids, list(reversed(ids)))):
            with Store(root / str(index)) as store:
                for item_id in ordered:
                    seed(store, item_id, date="2026-11-15T00:00:00Z")
                result = request((store, config), "recall", project="/repo", limit=limit)
                assert result["estimated_tokens"] <= limit
                expected = (len(canonical(result["items"]).encode("utf-8")) + 3) // 4
                assert result["estimated_tokens"] == (expected if result["items"] else 0)
                outputs.append(result)
    assert outputs[0] == outputs[1]


@settings(max_examples=15, deadline=None)
@given(
    st.lists(st.sampled_from(["one", "two", "three"]), unique=True, min_size=1),
    st.sampled_from(["asserted", "hedged", "tentative"]),
    st.sampled_from(["available", "unavailable", "changed"]),
)
def test_property_exchange_is_stable(ids, modality, availability):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = load_config(root / "config.toml")
        with Store(root / "source") as source, Store(root / "target") as target:
            for item_id in ids:
                seed(source, item_id, status="dismissed" if item_id == "one" else "active")
            source.connection.execute(
                "UPDATE observations SET modality=?,availability=?", (modality, availability)
            )
            forget_session(source, "claude", ids[-1])
            exported = request((source, config), "export")["export"]
            assert request((target, config), "import", **{"export": exported})["ok"]
            assert request((target, config), "export")["export"] == exported
            assert not request((target, config), "import", **{"export": exported})["changed"]


def test_feedback_learning_disposition_and_unknown_revisions(api_env):
    store, _ = api_env
    seed(store)
    store.connection.execute(
        "INSERT INTO learning(id,scope,evidence,next_step,outcome) "
        "VALUES ('learn',?,?,'Read','Understand')",
        (canonical({"projects": ["/repo"]}), canonical(["e-k"])),
    )
    assert request(
        api_env,
        "feedback",
        feedback_id="learn-addressed",
        learning_id="learn",
        disposition="addressed",
        reason="Finished the tutorial",
    )["ok"]
    assert all(i["id"] != "learn" for i in request(api_env, "recall", project="/repo")["items"])
    assert (
        request(
            api_env,
            "feedback",
            feedback_id="missing",
            revisions=[{"item_id": "missing", "version": 1, "usage": "cited"}],
        )["code"]
        == "not_found"
    )


@pytest.mark.parametrize("invalid", [None, [], "bad", 3, True])
def test_invalid_requests_always_return_a_versioned_error(api_env, invalid):
    response = handle_request(*api_env, invalid)
    assert response["dreaming_api"] == 1 and response["ok"] is False


def test_date_order_uses_chronology_and_confidence(api_env):
    store, _ = api_env
    seed(store, "later", date="2026-11-15T01:00:00+00:00", confidence="medium")
    seed(store, "earlier", date="2026-11-15T10:00:00+10:00", confidence="medium")
    seed(store, "high", date="2025-01-01T00:00:00Z", confidence="high")
    result = request(api_env, "recall", project="/repo", limit=10000)
    assert [i["id"] for i in result["items"]] == ["high", "later", "earlier"]


@settings(max_examples=15, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["project", "global", "other", "unresolved"]),
            st.sampled_from(["active", "candidate", "contested", "superseded", "dismissed"]),
        ),
        min_size=1,
        max_size=6,
    ),
    st.integers(min_value=0, max_value=10000),
)
def test_property_default_filters_and_utf8_budget(cases, limit):
    scopes = {
        "project": {"projects": ["/repo"]},
        "global": {"global": True},
        "other": {"projects": ["/other"]},
        "unresolved": {"unresolved": True},
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with Store(root / "home") as store:
            eligible = set()
            for index, (scope, status) in enumerate(cases):
                item_id = f"item-{index}"
                seed(store, item_id, scope=scopes[scope], status=status)
                store.connection.execute(
                    "UPDATE knowledge_items SET summary=? WHERE id=?",
                    ("Préférer Make 日本語", item_id),
                )
                if scope in {"project", "global"} and status == "active":
                    eligible.add(item_id)
            result = request(
                (store, load_config(root / "config.toml")), "recall", project="/repo", limit=limit
            )
            assert result["ok"] and result["estimated_tokens"] <= limit
            assert {item["id"] for item in result["items"]} <= eligible

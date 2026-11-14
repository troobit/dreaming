import json
import subprocess
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_selection import NOW, Source, document, select

from dreaming.config import load_config
from dreaming.knowledge import (
    available_observations,
    available_run_records,
    change_is_eligible,
    forget_session,
    knowledge_for_use,
    pattern_statistics,
    rank_patterns,
    record_call_inputs,
    restore_session,
)
from dreaming.sessions import (
    SessionError,
    SessionScope,
    attribute_session,
    canonical_project,
    mark_read,
    reader_document,
    reader_parts,
    register_session,
    select_sessions,
)
from dreaming.store import Store


@pytest.fixture
def evidence_env(tmp_path):
    config = load_config(
        tmp_path / "config.toml",
        overrides={
            "classification": {"rules": [{"provider": "lmstudio", "local": True}]},
            "domains": {"sources": {"claude": "software"}},
        },
    )
    with Store(tmp_path / "private") as store:
        yield config, store


def test_attribution_models_locality_and_unmapped_sources(evidence_env):
    config, store = evidence_env
    doc = document("s", provider="lmstudio", model="first", project="/missing/project")
    doc = replace(doc, entries=(replace(doc.entries[0], model="next"), *doc.entries[1:]))
    attributed = attribute_session(doc, store, config)
    assert attributed.harness == "claude"
    assert attributed.locality == "local"
    assert attributed.domain == "software"
    assert attributed.workflow is None
    assert attributed.unmapped_origin
    assert not attributed.unmapped_domain
    selected = select(Source(doc), evidence_env).sessions[0]
    entries = reader_document(store, selected, config).entries
    assert [e.model for e in entries] == ["next", "first", "first"]
    unknown = attribute_session(document("u", source="unmapped"), store, config)
    assert unknown.locality == "unknown" and unknown.unmapped_domain and unknown.interactive


def test_worktrees_count_as_parent_and_named_repo_scope(tmp_path, evidence_env):
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True)

    git("init", "-b", "main", str(repo))
    git(
        "-C",
        str(repo),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    git("-C", str(repo), "worktree", "add", "-b", "feature", str(worktree))
    assert canonical_project(str(worktree)) == str(repo.resolve())
    config, store = evidence_env
    config = replace(config, repositories={"named": {"path": str(repo)}})
    result = select_sessions(
        Source(document("s", project=str(worktree))),
        store,
        config,
        run_id="r",
        scope=SessionScope(project="named"),
        now=NOW,
    )
    assert len(result.sessions) == 1


def test_noninteractive_prompts_and_registered_user_quotes(evidence_env):
    config, store = evidence_env
    for source in ("codex-exec", "lmstudio"):
        selected = select(Source(document(source, source=source)), evidence_env).sessions[0]
        assert all(e.role == "tool" for e in reader_document(store, selected, config).entries)
    doc = document("s")
    assert register_session(
        store,
        source="claude",
        session_id="s",
        tool="orbit",
        run_ref="r",
        workflow="review",
        project="/missing/repo",
        user_quote_entries=("entry:1",),
    )
    before = store.connection.total_changes
    assert not register_session(
        store,
        source="claude",
        session_id="s",
        tool="orbit",
        run_ref="r",
        workflow="review",
        project="/missing/repo",
        user_quote_entries=("entry:1",),
    )
    assert store.connection.total_changes == before
    selected = select(Source(doc), evidence_env, scope=SessionScope(workflow="review")).sessions[0]
    assert [e.role for e in reader_document(store, selected, config).entries] == [
        "tool",
        "user",
        "tool",
    ]
    assert attribute_session(doc, store, config).project == "/missing/repo"
    assert not select(Source(doc), evidence_env, scope=SessionScope(workflow="other")).sessions


def seed_evidence(store, *, count=3):
    conn = store.connection
    conn.execute("INSERT INTO patterns(id,key) VALUES ('p','preference')")
    for i in range(count):
        selected = select(
            Source(document(f"s{i}", thread_id=f"t{i}")),
            (load_config(store.path.parent / "config.toml"), store),
        ).sessions[0]
        mark_read(store, selected, load_config(store.path.parent / "config.toml"))
        conn.execute(
            "INSERT INTO observations(id,source,session_id,entry_id,source_location,"
            "representation_version,redacted_quote,kind,modality,outcome,lens_id,lens_version,"
            "pattern_id,backend,project,observed_at) "
            "VALUES (?, 'claude', ?, 'entry:0', 'entry:0',1,'quote','user_statement',"
            "'asserted','unknown','preferences','1','p','fake',?,'2026-11-10T12:00:00Z')",
            (f"o{i}", f"s{i}", f"/project/{i}"),
        )
    evidence = json.dumps([f"o{i}" for i in range(count)])
    conn.execute(
        "INSERT INTO knowledge_items(id,kind,scope,status,summary,confidence,confidence_basis,"
        "supporting_evidence,actor,pattern_id) VALUES ('k','preference','{}','active','Prefer X',"
        "'high','observed',?,'dream','p')",
        (evidence,),
    )
    conn.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
    conn.execute(
        "INSERT INTO changes(id,run_id,pattern_id,repo,evidence) "
        "VALUES ('change','r','p','/repo',?)",
        (evidence,),
    )


def test_late_registration_reclassifies_observations_and_queues_reassessment(evidence_env):
    config, store = evidence_env
    seed_evidence(store, count=1)
    register_session(store, source="claude", session_id="s0", tool="orbit", run_ref="run")
    assert store.connection.execute("SELECT kind FROM observations").fetchone()[0] == "tool_prompt"
    item = knowledge_for_use(store, config)[0]
    assert item["pending_reassessment"]
    assert store.connection.execute("SELECT interactive FROM sessions_seen").fetchone()[0] == 0


def test_forget_all_copies_withhold_and_restore_without_data_loss(evidence_env):
    config, store = evidence_env
    seed_evidence(store)
    store.connection.execute("UPDATE observations SET kind='assistant_suggestion'")
    store.connection.execute(
        "INSERT INTO sessions_seen(source,session_id,thread_id) VALUES ('lmstudio','copy','t0')"
    )
    assert change_is_eligible(store, "change", config)
    assert forget_session(store, "lmstudio", "copy")
    assert (
        store.connection.execute("SELECT COUNT(*) FROM sessions_seen WHERE forgotten=1").fetchone()[
            0
        ]
        == 2
    )
    assert len(available_observations(store)) == 2
    assert pattern_statistics(store, "p")["sessions"] == 2
    assert not knowledge_for_use(store, config)
    assert not change_is_eligible(store, "change", config)
    assert not select(Source(document("s0", thread_id="t0")), evidence_env, reread=True).sessions
    with pytest.raises(SessionError, match="forgotten"):
        register_session(store, source="claude", session_id="s0", tool="orbit", run_ref="new")
    before = store.connection.total_changes
    assert not forget_session(store, "claude", "s0")
    assert store.connection.total_changes == before
    assert restore_session(store, "claude", "s0")
    assert len(available_observations(store)) == 3
    assert knowledge_for_use(store, config)[0]["pending_reassessment"]
    assert change_is_eligible(store, "change", config)
    assert store.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3


def test_reader_rechecks_midrun_forgetting_and_late_registration(evidence_env):
    config, store = evidence_env
    seed_evidence(store)
    selected = select(Source(document("s0", thread_id="t0")), evidence_env, reread=True).sessions[0]
    register_session(store, source="claude", session_id="s0", tool="orbit", run_ref="later")
    assert reader_document(store, selected, config).entries[0].role == "tool"
    mark_read(store, selected, config)
    assert (
        store.connection.execute(
            "SELECT interactive FROM sessions_seen WHERE session_id='s0'"
        ).fetchone()[0]
        == 0
    )
    forget_session(store, "claude", "s0")
    with pytest.raises(SessionError, match="forgotten"):
        reader_document(store, selected, config)
    with pytest.raises(SessionError, match="forgotten"):
        mark_read(store, selected, config)


def test_forgotten_input_excludes_dream_call_sessions_and_run_summaries(evidence_env):
    config, store = evidence_env
    seed_evidence(store)
    store.connection.execute("INSERT INTO steps(run_id,key,phase) VALUES ('r','read:1','Read')")
    store.connection.execute(
        "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,session_ref) "
        "VALUES ('c','r','read:1',1,'reader','fake','dream-session')"
    )
    record_call_inputs(store, "c", (("claude", "s0"),))
    dream = document("dream-session", project=str(store.path.parent / "runs" / "r"))
    assert select(Source(dream), evidence_env).sessions
    forget_session(store, "claude", "s0")
    result = select(Source(dream), evidence_env)
    assert not result.sessions and result.skipped[0].reason == "forgotten_input"
    assert not available_run_records(store)
    restore_session(store, "claude", "s0")
    assert select(Source(dream), evidence_env).sessions


def test_register_shared_json_file_and_discover_without_listing(tmp_path, evidence_env):
    config, store = evidence_env
    path = tmp_path / "registered.json"
    path.write_text(
        json.dumps(
            {
                "kind": "session",
                "schema_version": 1,
                "session": {"source": "custom", "id": "file", "thread_id": "corr"},
                "entries": [{"role": "user", "content": [{"type": "text", "text": "do it"}]}],
                "warnings": [],
            }
        )
    )
    register_session(store, document_path=path, tool="tool", run_ref="r")
    result = select(Source(), evidence_env)
    assert result.sessions[0].document.metadata.id == "file"
    forget_session(store, "custom", "file")
    with pytest.raises(SessionError, match="forgotten"):
        register_session(
            store,
            source="lmstudio",
            session_id="new-copy",
            correlation_id="corr",
            tool="tool",
            run_ref="r2",
        )


def test_unknown_forget_restore_error_and_restore_noop(evidence_env):
    _, store = evidence_env
    for operation in (forget_session, restore_session):
        with pytest.raises(SessionError, match="not_found.*missing"):
            operation(store, "claude", "missing")
    seed_evidence(store)
    before = store.connection.total_changes
    assert not restore_session(store, "claude", "s0")
    assert store.connection.total_changes == before


def test_reader_parts_use_smallest_fallback_and_keep_original_locations(evidence_env):
    config, store = evidence_env
    config = replace(
        config,
        roles={**config.roles, "reader": ("claude", "codex")},
        backends={
            **config.backends,
            "codex": replace(config.backends["codex"], context_tokens=300),
        },
    )
    doc = document("locations")
    doc = replace(doc, entries=(replace(doc.entries[0], unattributed=True), *doc.entries[1:]))
    selected = select(Source(doc), (config, store)).sessions[0]
    parts = reader_parts(store, selected, config, prompt_tokens=20, output_tokens=10)
    entries = [entry for part in parts for entry in json.loads(part.payload)["entries"]]
    assert [entry["location"] for entry in entries] == ["entry:1", "entry:2"]
    assert all(part.estimated_tokens <= 270 for part in parts)
    mark_read(store, selected, config)
    grown = replace(doc, entries=(*doc.entries, doc.entries[-1]))
    tail = select(Source(grown), (config, store)).sessions[0]
    assert tail.start_entry == 3
    assert (
        json.loads(reader_parts(store, tail, config)[0].payload)["entries"][0]["location"]
        == "entry:3"
    )


def test_approach_claim_loses_its_only_confirmed_outcome_on_forget(evidence_env):
    config, store = evidence_env
    seed_evidence(store)
    store.connection.execute("UPDATE knowledge_items SET kind='approach_worked'")
    store.connection.execute("UPDATE observations SET outcome='failed'")
    assert not change_is_eligible(store, "change", config)
    store.connection.execute("UPDATE observations SET outcome='check-confirmed' WHERE id='o0'")
    assert change_is_eligible(store, "change", config)
    forget_session(store, "claude", "s0")
    assert not knowledge_for_use(store, config)
    assert not change_is_eligible(store, "change", config)
    restore_session(store, "claude", "s0")
    assert change_is_eligible(store, "change", config)


def test_inferred_support_counts_matched_source_copies_once(evidence_env):
    config, store = evidence_env
    seed_evidence(store)
    store.connection.execute("UPDATE observations SET kind='assistant_suggestion'")
    store.connection.execute(
        "UPDATE sessions_seen SET thread_id='same' WHERE session_id IN ('s0','s1')"
    )
    assert pattern_statistics(store, "p")["sessions"] == 2
    assert not change_is_eligible(store, "change", config)


@settings(max_examples=20)
@given(st.lists(st.tuples(st.integers(0, 2), st.booleans()), max_size=20))
def test_forget_restore_counts_ranking_and_idempotence(tmp_path_factory, actions):
    with Store(tmp_path_factory.mktemp("forget")) as store:
        config = load_config(store.path.parent / "config.toml")
        seed_evidence(store)
        original = rank_patterns(store, config, now=NOW)
        forgotten = set()
        for i, forget in actions:
            operation = forget_session if forget else restore_session
            operation(store, "claude", f"s{i}")
            forgotten.add(i) if forget else forgotten.discard(i)
            expected = 3 - len(forgotten)
            assert pattern_statistics(store, "p")["sessions"] == expected
            ranked = rank_patterns(store, config, now=NOW)
            assert ranked[0]["sessions"] == expected if expected else not ranked
            if expected:
                assert ranked[0]["score"] == pytest.approx(expected * 2 + 2 ** (-4 / 30))
            before = store.connection.total_changes
            assert not operation(store, "claude", f"s{i}")
            assert store.connection.total_changes == before
        for i in forgotten:
            restore_session(store, "claude", f"s{i}")
        assert rank_patterns(store, config, now=NOW) == original

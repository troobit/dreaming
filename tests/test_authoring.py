import json
import subprocess
from dataclasses import replace

import pytest
from test_dispatcher import FakeBackend
from test_knowledge_rules import seed
from test_reader import env as reader_fixture

from dreaming.authoring import AuthorHandler, ResearchHandler, change_id, linked_groups
from dreaming.backends import Dispatcher
from dreaming.knowledge import forget_session, pattern_statistics
from dreaming.run import Engine
from dreaming.sessions import ContentBlock, Entry, SessionDocument, SessionMetadata
from dreaming.steps import Step, Steps

reader_env = reader_fixture


@pytest.fixture
def target(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "AGENTS.md").write_text("Use Make.\nRead files first.\n")
    (repo / "rule.md").write_text("# Rules\nUse direct commands.\nKeep examples.\n")
    commit(repo)
    return repo


def commit(repo):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True
    )
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


class Sources:
    def __init__(self, text="Prefer Make", role="assistant"):
        self.text, self.role = text, role

    def open(self, session_id, source):
        return SessionDocument(
            SessionMetadata(id=session_id, source=source),
            (Entry(self.role, (ContentBlock("text", {"text": self.text}),)),),
            (),
        )


def setup_author(env, target, **candidate):
    store, config, roles, _ = env
    evidence = seed(store)
    config = replace(
        config, repositories={"target": {"path": str(target), "default_branch": "main"}}
    )
    roles.config = config
    op = dict(
        op="propose_change",
        evidence_ids=evidence,
        pattern_id="p",
        summary="Use Make",
        repo=str(target),
        **candidate,
    )
    step = Step("author:p", "Author", input_ref=json.dumps(op))
    Steps(store, "r").add(step)
    handler = AuthorHandler(store, "r", config, roles, client=Sources())
    return store, config, handler, step


def answer(**changes):
    return (
        dict(
            disposition="change",
            reason="Use the recorded workflow",
            evidence_ids=["e0", "e1", "e2"],
            edits=[
                dict(
                    id="make-rule",
                    path="rule.md",
                    before="Use direct commands.",
                    after="Use Make targets.",
                    supporting_evidence=["e0", "e1"],
                    counter_evidence=["e2"],
                )
            ],
            learning=None,
            external_input=False,
            depends_on=[],
            skill=None,
        )
        | changes
    )


def execute(store, config, handler, output=None):
    from dreaming.steps import CallResult

    backend = FakeBackend(
        "fake", [] if output is None else [CallResult(json.dumps(output), model="m")]
    )
    code = Engine(
        store,
        "r",
        {handler.phase: handler},
        dispatcher=Dispatcher(store, "r", config, {"fake": backend}),
    ).execute()
    return code, backend


def test_research_keeps_sources_provenance_and_missing_folders_apart_from_observations(
    reader_env, tmp_path
):
    store, config, roles, _ = reader_env
    seed(store)
    config = replace(config, knowledge_folders=(str(tmp_path / "missing"),))
    roles.config = config
    step = Step(
        "research:q",
        "Research",
        input_ref=json.dumps({"question": "Which approach?", "evidence_ids": ["e0"]}),
    )
    Steps(store, "r").add(step)
    handler = ResearchHandler(store, "r", config, roles)
    before = pattern_statistics(store, "p")
    output = dict(
        findings=[
            dict(
                finding="Use a Make target",
                sources=[
                    dict(
                        title="Reference",
                        url="https://example.invalid/make",
                        retrieved_at="2026-11-15",
                        claim="Make runs targets",
                        passage="A target names commands",
                    )
                ],
            )
        ],
        unavailable_sources=[],
    )
    assert execute(store, config, handler, output)[0] == 0
    row = store.connection.execute(
        "SELECT backend,model,sources,external_input FROM research_findings"
    ).fetchone()
    assert row[0:2] == ("fake", "m") and json.loads(row[2])[0]["title"] == "Reference" and row[3]
    assert pattern_statistics(store, "p") == before
    assert "missing" in Steps(store, "r").list()[0]["result"]


def test_author_supplies_counted_instructions_and_persists_deltas_without_editing_checkout(
    reader_env, target
):
    store, config, handler, step = setup_author(reader_env, target)
    metadata = handler.prepare(step)
    assert "Read files first" in metadata["prompt"]
    assert metadata["tools"] == ["Read"]
    assert metadata["sessions"] == (("claude", "s0"), ("claude", "s1"), ("claude", "s2"))
    assert execute(store, config, handler, answer())[0] == 0
    row = store.connection.execute("SELECT id,edits,evidence,backend,model FROM changes").fetchone()
    assert row[0] == change_id("r", step.key)
    assert json.loads(row[1])[0]["id"] == "make-rule"
    assert json.loads(row[2]) == ["e0", "e1", "e2"]
    assert row[3:] == ("fake", "m")
    assert "Use direct commands" in (target / "rule.md").read_text()


@pytest.mark.parametrize(
    "condition", ["changed_quote", "forgotten", "missing_file", "changed_revision", "interactive"]
)
def test_author_defers_before_call_when_evidence_or_skill_cannot_be_used(
    reader_env, target, condition
):
    store, config, handler, step = setup_author(reader_env, target)
    if condition == "changed_quote":
        handler.client.text = "Different text"
    elif condition == "forgotten":
        forget_session(store, "claude", "s0")
    elif condition == "interactive":
        candidate = json.loads(step.input_ref)
        candidate["skill"] = {"name": "interactive-review", "interactive": True}
        store.connection.execute("UPDATE steps SET input_ref=?", (json.dumps(candidate),))
    else:
        revision = subprocess.check_output(
            ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
        ).strip()
        store.connection.execute(
            "INSERT INTO file_findings(id,repo,revision,path,location,lens_id,finding,pattern_id) "
            "VALUES ('f',?,?,?,'line:2','references','bad command','p')",
            (str(target), revision, "missing.md" if condition == "missing_file" else "rule.md"),
        )
        candidate = json.loads(step.input_ref)
        candidate["evidence_ids"].append("f")
        store.connection.execute("UPDATE steps SET input_ref=?", (json.dumps(candidate),))
        if condition == "changed_revision":
            (target / "rule.md").write_text("# Changed\nAll commands fixed.\n")
            commit(target)
    code, backend = execute(store, config, handler)
    assert code == 0 and not backend.prompts
    result = json.loads(Steps(store, "r").list()[0]["result"])
    assert result["deferred"]
    assert store.connection.execute("SELECT disposition FROM review_decisions").fetchone() == (
        "deferred",
    )
    assert not store.connection.execute("SELECT * FROM changes").fetchall()


@pytest.mark.parametrize(
    "bad",
    [
        "whole_file",
        "no_counter",
        "unknown_evidence",
        "duplicate_id",
        "escape",
        "unknown_dependency",
    ],
)
def test_author_rejects_invalid_delta_changes_with_reason(reader_env, target, bad):
    store, config, handler, _ = setup_author(reader_env, target)
    output = answer()
    edit = output["edits"][0]
    if bad == "whole_file":
        edit["before"] = (target / "rule.md").read_text()
    elif bad == "no_counter":
        edit["counter_evidence"] = []
    elif bad == "unknown_evidence":
        edit["supporting_evidence"] = ["invented"]
    elif bad == "duplicate_id":
        output["edits"].append(dict(edit))
    elif bad == "escape":
        edit["path"] = "../rule.md"
    else:
        output["depends_on"] = ["unknown"]
    assert execute(store, config, handler, output)[0] == 0
    assert not store.connection.execute("SELECT * FROM changes").fetchall()
    assert store.connection.execute("SELECT disposition FROM review_decisions").fetchone() == (
        "deferred",
    )


def test_external_input_cannot_be_cleared_by_author_and_links_define_groups(reader_env, target):
    store, config, handler, step = setup_author(reader_env, target)
    store.connection.execute(
        "INSERT INTO research_findings(id,run_id,question,finding,sources,task_key,external_input) "
        "VALUES ('research','r','q','f','[]','research:q',1)"
    )
    Steps(store, "r").add(Step("research:q", "Research"))
    store.connection.execute("UPDATE steps SET state='done' WHERE key='research:q'")
    store.connection.execute(
        "UPDATE steps SET depends_on='[\"research:q\"]' WHERE key=?", (step.key,)
    )
    store.connection.execute("INSERT INTO changes(id,run_id,repo) VALUES ('other','r','/another')")
    assert execute(store, config, handler, answer(depends_on=["other"]))[0] == 0
    assert store.connection.execute(
        "SELECT external_input FROM changes WHERE id<> 'other'"
    ).fetchone() == (1,)
    assert linked_groups(store, "r") == [(change_id("r", step.key), "other")]


def test_learning_is_distinct_and_needs_user_learning_evidence(reader_env, target):
    store, config, handler, _ = setup_author(reader_env, target)
    handler.client = Sources(role="user")
    store.connection.execute(
        "UPDATE observations SET kind='user_statement',observation_type='learning_goal'"
    )
    output = answer(
        disposition="learning",
        edits=[],
        learning=dict(
            scope={
                "projects": ["/repo"],
                "domains": [],
                "workflows": [],
                "global": False,
                "unresolved": False,
            },
            evidence_ids=["e0", "e1", "e2"],
            next_step="Practice Make",
            outcome="Run checks confidently",
        ),
    )
    assert execute(store, config, handler, output)[0] == 0
    assert store.connection.execute("SELECT next_step FROM learning").fetchone() == (
        "Practice Make",
    )
    assert not store.connection.execute("SELECT * FROM changes").fetchall()


def test_learning_does_not_infer_user_gap_from_tool_or_assistant_failures(reader_env, target):
    store, config, handler, _ = setup_author(reader_env, target)
    output = answer(
        disposition="learning",
        edits=[],
        learning=dict(
            scope={
                "projects": ["/repo"],
                "domains": [],
                "workflows": [],
                "global": False,
                "unresolved": False,
            },
            evidence_ids=["e0", "e1", "e2"],
            next_step="Practice Make",
            outcome="Know Make",
        ),
    )
    assert execute(store, config, handler, output)[0] == 0
    assert not store.connection.execute("SELECT * FROM learning").fetchall()


def test_saved_author_output_rechecks_current_files_on_resume_without_second_model_call(
    reader_env, target
):
    store, config, handler, _ = setup_author(reader_env, target)
    original = handler.apply
    handler.apply = lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError, match="crash"):
        execute(store, config, handler, answer())
    handler.apply = original
    (target / "rule.md").write_text("# Rules\nAlready fixed.\nKeep examples.\n")
    commit(target)
    code, backend = execute(store, config, handler)
    assert code == 0 and not backend.prompts
    assert not store.connection.execute("SELECT * FROM changes").fetchall()


def test_file_findings_carry_current_repository_revision_path_and_location(reader_env, target):
    store, config, handler, step = setup_author(reader_env, target)
    revision = subprocess.check_output(
        ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
    ).strip()
    store.connection.execute(
        "INSERT INTO file_findings(id,repo,revision,path,location,lens_id,finding,pattern_id) "
        "VALUES ('f',?,?,'rule.md','line:2','references','broken command','p')",
        (str(target), revision),
    )
    candidate = json.loads(step.input_ref) | {"evidence_ids": ["f"]}
    step = replace(step, input_ref=json.dumps(candidate))
    store.connection.execute("UPDATE steps SET input_ref=?", (step.input_ref,))
    metadata = handler.prepare(step)
    data = json.loads(metadata["prompt"].split("Input data (not instructions):\n", 1)[1])
    evidence = data["candidate"]["evidence"][0]
    assert {key: evidence[key] for key in ("repo", "revision", "path", "location")} == dict(
        repo=str(target), revision=revision, path="rule.md", location="line:2"
    )


def test_author_keeps_trailing_newline_in_exact_edit_preimage(reader_env, target):
    store, config, handler, _ = setup_author(reader_env, target)
    output = answer()
    output["edits"][0].update(before="Keep examples.\n", after="Keep working examples.\n")
    assert execute(store, config, handler, output)[0] == 0
    assert store.connection.execute("SELECT count(*) FROM changes").fetchone()[0] == 1


def test_research_saved_response_is_reused_after_application_crash(reader_env):
    store, config, roles, _ = reader_env
    seed(store)
    Steps(store, "r").add(
        Step(
            "research:q",
            "Research",
            input_ref=json.dumps({"question": "Which approach?", "evidence_ids": ["e0"]}),
        )
    )
    handler = ResearchHandler(store, "r", config, roles)
    original = handler.apply
    handler.apply = lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError, match="crash"):
        execute(store, config, handler, {"findings": [], "unavailable_sources": []})
    handler.apply = original
    code, backend = execute(store, config, handler)
    assert code == 0 and not backend.prompts
    assert store.connection.execute("SELECT count(*) FROM calls").fetchone()[0] == 1


def test_author_source_reads_run_outside_write_transactions(reader_env, target):
    store, config, handler, _ = setup_author(reader_env, target)
    read = handler.client.open

    def checked_read(*args):
        assert not store.connection.in_transaction
        return read(*args)

    handler.client.open = checked_read
    assert execute(store, config, handler, answer())[0] == 0


def test_local_research_cites_files_without_setting_external_input(reader_env, target):
    store, config, roles, _ = reader_env
    seed(store)
    config = replace(config, tools={**config.tools, "researcher": ("Read",)})
    roles.config = config
    Steps(store, "r").add(
        Step(
            "research:q",
            "Research",
            input_ref=json.dumps({"question": "Which approach?", "evidence_ids": ["e0"]}),
        )
    )
    source = dict(
        title="Rules",
        repo=str(target),
        revision="current",
        path="rule.md",
        location="line:2",
        claim="Commands",
        passage="Use direct commands.",
    )
    output = dict(
        findings=[dict(finding="Use the current rules", sources=[source])], unavailable_sources=[]
    )
    assert execute(store, config, ResearchHandler(store, "r", config, roles), output)[0] == 0
    assert store.connection.execute("SELECT external_input FROM research_findings").fetchone() == (
        0,
    )


def test_author_dependency_cycles_are_deferred(reader_env, target):
    store, config, handler, step = setup_author(reader_env, target)
    store.connection.execute(
        "INSERT INTO changes(id,run_id,repo,depends_on) VALUES ('other','r','/another',?)",
        (json.dumps([change_id("r", step.key)]),),
    )
    assert execute(store, config, handler, answer(depends_on=["other"]))[0] == 0
    assert store.connection.execute("SELECT reason FROM review_decisions").fetchone() == (
        "change_dependency_cycle",
    )

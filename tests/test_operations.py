import json

import pytest

from dreaming.api import handle_request
from dreaming.config import load_config
from dreaming.run import RunSession
from dreaming.store import Store


@pytest.fixture
def env(tmp_path, instruction_repo, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    path = tmp_path / "config.toml"
    path.write_text(f'[instructions]\nrepository="{instruction_repo[0]}"\n')
    monkeypatch.setattr(RunSession, "detach", lambda self, target, run_id: 123)
    with Store(home) as store:
        yield store, load_config(path)


def request(env, op, **fields):
    return handle_request(*env, {"dreaming_api": 1, "op": op, **fields})


def test_start_status_resume_keeps_identity_snapshot_and_overrides(env):
    result = request(env, "dream.start", requester="scheduler", scope={"workflow": "review"})
    assert result["ok"], result
    rid = result["run_id"]
    record = request(env, "dream.status", run_id=rid)["run"]
    assert record["requester"] == "scheduler"
    assert json.loads(record["overrides"])["scope"] == {"workflow": "review"}
    assert request(env, "dream.resume", run_id=rid, max_tokens=10)["code"] == "invalid_request"
    resumed = request(env, "dream.resume", run_id=rid, backends={"reader": ["codex"]})
    assert resumed["ok"] and resumed["run_id"] == rid
    assert request(env, "dream.status", run_id="missing")["code"] == "not_found"


def test_waiting_status_and_unknown_session_errors(env):
    rid = request(env, "dream.start")["run_id"]
    env[0].connection.execute(
        "UPDATE runs SET status='waiting',wait_until='2030-01-01T00:00:00Z',"
        "notice='fallback_on_usage_limit can use the next backend' WHERE id=?",
        (rid,),
    )
    status = request(env, "dream.status", run_id=rid)["run"]
    assert status["status"] == "waiting" and status["wait_until"]
    assert "fallback_on_usage_limit" in status["notice"]
    assert (
        request(env, "session.forget", source="claude", session_id="missing")["code"] == "not_found"
    )


def test_registration_forget_restore_are_idempotent(env):
    fields = dict(tool="scheduler", run_ref="job", source="claude", session_id="s")
    assert request(env, "session.register", **fields)["changed"]
    assert not request(env, "session.register", **fields)["changed"]
    fields = dict(source="claude", session_id="s")
    assert request(env, "session.forget", **fields)["changed"]
    assert not request(env, "session.forget", **fields)["changed"]
    assert not request(env, "session.register", **dict(tool="x", run_ref="j", **fields))["ok"]
    assert request(env, "session.restore", **fields)["changed"]
    assert not request(env, "session.restore", **fields)["changed"]


def test_discard_removes_run_owned_rows_but_keeps_tombstone(env):
    rid = request(env, "dream.start")["run_id"]
    env[0].connection.execute("INSERT INTO steps(run_id,key,phase) VALUES (?,'x','Read')", (rid,))
    assert request(env, "dream.discard", run_id=rid)["ok"]
    assert request(env, "dream.status", run_id=rid)["run"]["status"] == "discarded"
    assert env[0].connection.execute("SELECT count(*) FROM steps").fetchone()[0] == 0


def test_start_held_lock_names_existing_run_without_changing_state(env):
    rid = request(env, "dream.start")["run_id"]
    with RunSession(env[0].path.parent):
        before = list(env[0].connection.iterdump())
        result = request(env, "dream.start")
        assert result["code"] == "already_running" and rid in result["message"]
        assert list(env[0].connection.iterdump()) == before


@pytest.mark.parametrize("scope", [{"unknown": "x"}, {"project": 2}])
def test_invalid_start_scope_does_not_create_run(env, scope):
    assert request(env, "dream.start", scope=scope)["code"] == "invalid_scope"
    assert env[0].connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_explicit_resume_of_exhausted_run_does_not_create_another(env):
    rid = request(env, "dream.start")["run_id"]
    env[0].connection.execute("UPDATE runs SET statistics=?", ('{"active_seconds":999999}',))
    assert not request(env, "dream.resume", run_id=rid)["ok"]
    assert env[0].connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_cli_start_can_automatically_resume_without_inventing_overrides(env, capsys):
    from dreaming.cli import main

    args = ["--config", str(env[1].source), "start"]
    assert main(args) == 0
    rid = json.loads(capsys.readouterr().out)["run_id"]
    assert main(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["run_id"] == rid and response["resumed"]


def test_restore_selection_preserves_source_copies_filtered_lenses_and_subject(env):
    from dataclasses import asdict
    from pathlib import Path
    from types import SimpleNamespace

    from dreaming.config import Lens
    from dreaming.operations import restore_selection
    from dreaming.sessions import parse_document

    document = parse_document((Path(__file__).parent / "fixtures/apsis/claude.json").read_text())
    meta = document.metadata
    roles = SimpleNamespace(lenses=(Lens("chosen", "1", "x", ()), Lens("other", "1", "x", ())))
    ref = dict(
        source=meta.source,
        session_id=meta.id,
        copies=[asdict(meta)],
        start=1,
        copy_entry_counts=[2],
        subject="dreaming",
        lenses=["chosen"],
    )
    selected = restore_selection(
        env[0],
        env[1],
        roles,
        SimpleNamespace(open=lambda *args: document),
        {"selection": json.dumps([ref])},
    ).sessions[0]
    assert selected.start_entry == 1 and selected.copy_entry_counts == (2,)
    assert selected.subject == "dreaming" and not selected.attribution.interactive
    assert [lens.id for lens in selected.lenses] == ["chosen"]


def test_worker_schedules_adoption_and_always_writes_report(env, tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from types import SimpleNamespace

    from dreaming.operations import work
    from dreaming.sessions import Listing
    from dreaming.steps import CallResult

    repo = env[1].instruction_repo
    shutil.rmtree(repo / "instructions")
    shutil.copytree(Path(__file__).parents[1] / "instructions", repo / "instructions")
    from test_adopt import git

    git(repo, "add", ".")
    git(repo, "commit", "-m", "full instruction set")
    rid = request(env, "dream.start", requester="scheduler")["run_id"]
    monkeypatch.setattr(
        "dreaming.sessions.ApsisClient",
        lambda **kw: SimpleNamespace(
            list=lambda: Listing(schema_version=1, sessions=(), scope="all", warnings=())
        ),
    )

    class Backend:
        supports_tools = False
        context_tokens = 200000

        def __init__(self, name, *args):
            self.name = name

        def probe(self):
            return SimpleNamespace(compatible=True)

        def call(self, role, prompt, schema, tools):
            return CallResult('{"operations":[]}', tokens_in=10, tokens_out=5)

    monkeypatch.setattr("dreaming.backends.claude.ClaudeBackend", Backend)
    monkeypatch.setattr("dreaming.backends.codex.CodexBackend", Backend)
    assert work(env[0], rid) == 0
    assert (
        env[0].connection.execute("SELECT state FROM steps WHERE key='adopt'").fetchone()[0]
        == "done"
    )
    assert (env[0].path.parent / "reports" / f"{rid}.html").exists()
    inputs = json.loads(env[0].connection.execute("SELECT input_ref FROM calls").fetchone()[0])
    assert inputs["role"] == "reviewer" and inputs["schema"] and inputs["tools"] == []

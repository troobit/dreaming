"""Portable dream: fresh home, no agentic-coding checkout, skills or target repository."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dreaming.api import handle_request
from dreaming.config import load_config
from dreaming.run import RunSession
from dreaming.sessions import ContentBlock, Entry, Listing, SessionDocument, SessionMetadata
from dreaming.steps import CallResult
from dreaming.store import Store

MARKER = "Input data (not instructions):\n"
TEXTS = {"s1": "Prefer Make targets for builds.", "s2": "Always run the tests first."}
WARNING = {"code": "source_unavailable", "message": "codex-exec source is not installed"}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


class Source:
    def __init__(self):
        self.documents = {
            sid: SessionDocument(
                SessionMetadata(
                    source="claude", id=sid, created_at="2026-11-10T12:00:00Z", project="/work/app"
                ),
                (Entry("user", (ContentBlock("text", {"type": "text", "text": text}),)),),
                (),
            )
            for sid, text in TEXTS.items()
        }

    def list(self):
        return Listing(
            {"all_projects": True}, tuple(d.metadata for d in self.documents.values()), (WARNING,)
        )

    def open(self, session_id, source):
        return self.documents[session_id]


class Backend:
    """Answers each role from its own input; evidence identifiers are read from the prompt."""

    supports_tools = False
    context_tokens = 200000
    prompts = []

    def __init__(self, name, *args):
        self.name = name

    def probe(self):
        return SimpleNamespace(compatible=True)

    def call(self, role, prompt, schema, tools):
        type(self).prompts.append((role, prompt))
        data = json.loads(prompt.split(MARKER, 1)[1])
        if role == "reader":
            sid = next(s for s, text in TEXTS.items() if text in prompt)
            observation = dict(
                source="claude",
                session_id=sid,
                entry_id="entry:0",
                source_location="entry:0",
                representation_version=1,
                quote=TEXTS[sid],
                kind="preference",
                modality="asserted",
                outcome="unknown",
                outcome_evidence=[],
                lens_id="preferences",
                lens_version="1",
                pattern_key="preferences: " + sid,
                subject="user",
            )
            return CallResult(json.dumps({"observations": [observation]}), 10, 5, model="m")
        assert role == "reviewer"
        operations = []
        for cluster in data["new_clusters"]:
            ids = [e["id"] for e in cluster["evidence"]]
            operations.append(
                dict(
                    op="create",
                    item=dict(
                        kind="preference",
                        scope=dict(
                            domains=[],
                            projects=["/work/app"],
                            workflows=[],
                            **{"global": False},
                            unresolved=False,
                        ),
                        summary="Remembered: " + cluster["evidence"][0]["quote"],
                        confidence="medium",
                        confidence_basis="Stated by the user",
                        supporting_evidence=ids,
                        contradicting_evidence=[],
                        lens_id="preferences",
                    ),
                )
            )
        return CallResult(json.dumps({"operations": operations}), 10, 5, model="m")


@pytest.fixture
def portable(tmp_path, monkeypatch):
    repo = tmp_path / "somewhere-else" / "instructions"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    shutil.copytree(Path(__file__).parents[1] / "instructions", repo / "instructions")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "instructions")
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'auto_apply = false\n[instructions]\nrepository = "{repo}"\n'
        "[thresholds]\nmin_sessions = 1\n"
    )
    monkeypatch.setattr(RunSession, "detach", lambda self, target, run_id: 1)
    monkeypatch.setattr("dreaming.sessions.ApsisClient", lambda **kw: Source())
    monkeypatch.setattr("dreaming.backends.claude.ClaudeBackend", Backend)
    monkeypatch.setattr("dreaming.backends.codex.CodexBackend", Backend)
    Backend.prompts = []
    with Store(home) as store:
        yield store, load_config(config_path), tmp_path


def call(env, op, **fields):
    return handle_request(env[0], env[1], {"dreaming_api": 1, "op": op, **fields})


def test_dream_retains_knowledge_writes_report_and_round_trips_through_a_fresh_store(portable):
    from dreaming.operations import work

    store, config, tmp_path = portable
    env = (store, config)
    started = call(env, "dream.start", open_browser=False)
    assert started["ok"], started
    rid = started["run_id"]
    assert work(store, rid) == 0, store.connection.execute("SELECT notice FROM runs").fetchone()

    # Knowledge retained, with no skills, target repository or sync command involved.
    items = call(env, "knowledge.recall", project="/work/app")["items"]
    assert {i["summary"] for i in items} == {"Remembered: " + t for t in TEXTS.values()}
    assert store.connection.execute("SELECT count(*) FROM changes").fetchone() == (0,)

    # The report is written and names the unavailable optional integration.
    report = (store.path.parent / "reports" / f"{rid}.html").read_text()
    assert "codex-exec source is not installed" in report

    # Dismiss one item and forget the other session, then export.
    dismissed = next(i for i in items if "tests" in i["summary"])
    version = call(env, "knowledge.get", id=dismissed["id"])["item"]["version"]
    assert call(
        env,
        "knowledge.feedback",
        feedback_id="drop",
        revisions=[{"item_id": dismissed["id"], "version": version, "usage": "cited"}],
        dismiss=True,
        reason="Not wanted",
        basis="user-confirmed",
    )["ok"]
    assert call(env, "session.forget", source="claude", session_id="s1")["changed"]
    exported = call(env, "knowledge.export")["export"]

    with Store(tmp_path / "second-home") as second:
        other = (second, config)
        assert call(other, "knowledge.import", export=exported)["ok"]
        assert call(other, "knowledge.export")["export"] == exported
        assert call(other, "knowledge.import", export=exported)["changed"] is False
        assert call(other, "knowledge.get", id=dismissed["id"])["item"]["status"] == "dismissed"
        forgotten = second.connection.execute(
            "SELECT forgotten FROM sessions_seen WHERE session_id='s1'"
        ).fetchone()
        assert forgotten == (1,)
        assert {
            row[0] for row in second.connection.execute("SELECT availability FROM observations")
        } == {"available"}

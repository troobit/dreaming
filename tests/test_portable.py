"""The portable boundary, end to end (14.1, 14.3, 14.4).

A dream runs through the `dream` command and its detached process with a
session source, a backend and a store outside any agentic-coding checkout, no
skills installed and no target repository. The source is the fake `apsis`
serving documents from a directory, the backend the fake `claude` answering
through `portable_model.py`, and the instruction set a repository of its own.
The knowledge it retains is exported and imported into a second fresh store.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from runkit import instruction_set
from sessiondocs import make_doc, make_entry

from dreaming import config

MODEL = Path(__file__).with_name("portable_model.py")


def dream(*args, request=None):
    """The `dream` command in a process of its own, as the installed one runs."""
    return subprocess.run(
        [sys.executable, "-I", "-m", "dreaming.cli", *args],
        input="" if request is None else json.dumps(request),
        capture_output=True,
        text=True,
        timeout=120,
    )


def api(op, **fields):
    done = dream("api", request={"dreaming_api": 1, "op": op, **fields})
    response = json.loads(done.stdout)
    assert response["ok"], response
    return response


def write_session(directory, sid, project, days_ago, text):
    when = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = make_doc(
        "claude",
        sid,
        when,
        project=project,
        origin="cli",
        entries=[
            make_entry("user", text, when, id=f"{sid}-u1"),
            make_entry("assistant", "Noted.", when, id=f"{sid}-a1"),
        ],
    )
    (directory / f"{sid}.json").write_text(json.dumps(doc))


def configure(home, root, instructions):
    """No repositories, a knowledge folder that does not exist, no shared MCP
    definitions, and an LM Studio fallback for the reader that is not running."""
    (home / "config.toml").write_text(
        f"knowledge_folders = [{json.dumps(str(root / 'missing-notes'))}]\n"
        "[instructions]\n"
        f"repo = {json.dumps(str(instructions))}\n"
        "[report]\n"
        "open_browser = false\n"
        "[backends.claude]\n"
        'harness = "claude"\n'
        "context_tokens = 200000\n"
        "[backends.local]\n"
        'harness = "lmstudio"\n'
        'model = "local-model"\n'
        'base_url = "http://127.0.0.1:9"\n'
        "context_tokens = 32768\n"
        "[roles.reader]\n"
        'backends = ["claude", "local"]\n'
    )


@pytest.fixture
def portable(tmp_path, fakes):
    """A fresh home with its source and backend, all outside this checkout."""
    root = tmp_path / "portable"
    sources = root / "sessions"
    sources.mkdir(parents=True)
    project = str(root / "work" / "app")
    for sid, days_ago, text in (
        ("s-uv-1", 4, "Use uv, not pip. key: uses uv"),
        ("s-uv-2", 3, "Use uv, not pip. key: uses uv"),
        ("s-tabs", 2, "Indent with tabs. key: uses tabs"),
        ("s-rust", 1, "I want to learn Rust lifetimes. key: learning rust"),
    ):
        write_session(sources, sid, project, days_ago, text)
    fakes.scenario(sessions_dir=str(sources), responder=str(MODEL))
    home = config.ensure_home()
    configure(home, root, instruction_set(root))
    for path in (root, home):
        assert config.containing_repository(path) is None
    assert not (Path.home() / ".claude").exists()  # no skills installed
    return {"root": root, "sources": sources, "project": project, "home": home}


def contents(response):
    return sorted(item["content"] for item in response["items"])


def test_a_dream_retains_knowledge_that_moves_to_a_fresh_store(portable, monkeypatch):
    home, project = portable["home"], portable["project"]

    # A dream retains knowledge, researches and recommends, and writes its
    # report, though the knowledge folder, the shared MCP definitions behind
    # the researcher's web tools and the LM Studio backend are all missing.
    done = dream("run", "--wait")
    assert done.returncode == 0, done.stdout + done.stderr
    status = api("dream.status")
    assert (status["status"], status["requester"]) == ("complete", "user")
    # Four reads, the review, research, authoring and the adoption plan.
    assert status["steps"] == {"done": 8}
    recalled = api("knowledge.recall", scope={"project": project})
    assert contents(recalled) == [
        "Learn how Rust lifetimes work",
        "The user uses tabs.",
        "The user uses uv.",
    ]
    by_content = {item["content"]: item for item in recalled["items"]}
    uv, tabs = by_content["The user uses uv."], by_content["The user uses tabs."]
    assert uv["scope"]["projects"] == [project]

    page = Path(api("dream.report")["report"])
    assert page.is_relative_to(home)
    html = page.read_text()
    assert "The user uses uv." in html
    assert "Learn how Rust lifetimes work" in html
    for unavailable in (
        "backend local",  # LM Studio is not running
        "127.0.0.1:9",
        "knowledge folder " + str(portable["root"] / "missing-notes"),
        "devtools.internet_search for the researcher",
    ):
        assert unavailable in html
    assert "Under the unassigned domain" in html  # no domain mapping (2.3)

    # The source of one session goes away, and the next dream marks its
    # evidence unavailable (13.7).
    (portable["sources"] / "s-uv-1.json").unlink()
    done = dream("run", "--wait")
    assert done.returncode == 0, done.stdout + done.stderr
    evidence = api("knowledge.get", id=uv["id"])["item"]["evidence"]["supporting"]
    assert sorted(ref["availability"] for ref in evidence) == [
        "available",
        "unavailable",
    ]

    # The user dismisses one item and forgets one session.
    api(
        "knowledge.feedback",
        feedback_id="fb-1",
        correction={
            "item_id": tabs["id"],
            "base_version": tabs["revision"],
            "dismiss": True,
            "reason": "spaces now",
        },
    )
    assert api("session.forget", session_id="s-uv-2")["changed"]
    exported = api("knowledge.export")["export"]

    # A second fresh store takes the export; importing it again adds nothing.
    monkeypatch.setenv("DREAMING_HOME", str(portable["root"] / "second-home"))
    api("knowledge.import", export=exported)
    assert api("knowledge.export")["export"] == exported
    api("knowledge.import", export=exported)
    assert api("knowledge.export")["export"] == exported

    # Identifiers, scopes, availability, dismissal and forgetting all hold.
    item = api("knowledge.get", id=uv["id"])["item"]
    assert (item["scope"], item["status"]) == (uv["scope"], "active")
    shown = item["evidence"]["supporting"]
    assert [ref["availability"] for ref in shown] == ["unavailable"]
    assert api("knowledge.get", id=tabs["id"])["item"]["status"] == "dismissed"
    assert contents(api("knowledge.recall", scope={"project": project})) == [
        "Learn how Rust lifetimes work",
        "The user uses uv.",
    ]
    forgotten = {
        row["session_id"]: row["forgotten"]
        for row in exported["tables"]["sessions_seen"]
    }
    assert forgotten == {"s-rust": 0, "s-tabs": 0, "s-uv-1": 0, "s-uv-2": 1}

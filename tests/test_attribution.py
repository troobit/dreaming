"""Attribution, interactivity and registration (2.2, 2.3, 2.9, 2.10, 14.8)."""

import json
import subprocess

import pytest
from sessiondocs import (
    NOW,
    StubApsis,
    add_item,
    add_observation,
    add_run,
    add_seen,
    days_ago,
    fixture,
    make_doc,
    pending,
    select,
)

from dreaming import config, sessions
from dreaming.errors import DreamingError

SERVED = "0199cccc-0000-7000-8000-000000000003"
LMSTUDIO_ID = "lmstudio-thread-130d11ea3c18a18b3e2af1056ebe36cdab4e4715e0db09df"


@pytest.fixture
def cfg():
    return config.build_config(
        {
            "domains": {
                "projects": {"/work/repo": "software"},
                "sources": {"lmstudio": "general"},
            }
        }
    )


def _only(db, apsis, cfg, home):
    (session,) = select(db, apsis, cfg, home).sessions
    return session


def _roles(session):
    return [entry.role for entry in session.read_entries()]


def _register(db, *entries, tool="orbit", run_ref="run-1"):
    return sessions.register_sessions(db, tool, run_ref, list(entries), now=NOW)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


# Fields (2.2, 2.3)


def test_a_session_is_attributed_from_its_recorded_metadata(db, home, cfg):
    attribution = _only(db, StubApsis(fixture("session-codex")), cfg, home).attribution
    assert attribution.harness == "codex"
    assert (attribution.provider, attribution.locality) == ("openai", "cloud")
    assert attribution.model == "example-model-a"
    assert attribution.project == "/work/example-repo"
    assert attribution.started_at == "2026-10-01T09:00:00.000Z"
    assert attribution.workflow is None


def test_the_model_is_attributed_per_entry_else_the_first_recorded(db, home, cfg):
    session = _only(db, StubApsis(fixture("session-codex")), cfg, home)
    models = [session.entry_model(entry) for entry in session.entries()]
    assert models == ["example-model-a", "example-model-a", "example-model-b"]


def test_fields_a_source_cannot_supply_stay_unknown(db, home, cfg):
    attribution = _only(
        db, StubApsis(fixture("session-codex-exec")), cfg, home
    ).attribution
    assert attribution.harness == "codex"
    assert attribution.model is None
    assert attribution.provider is None
    assert attribution.project is None
    assert attribution.started_at is None
    assert attribution.locality == "unknown"
    assert attribution.domain is None


def test_local_models_are_classified_by_their_provider(db, home, cfg):
    session = _only(db, StubApsis(fixture("session-lmstudio")), cfg, home)
    assert session.attribution.locality == "local"


def test_a_worktree_or_subdirectory_counts_as_its_repository(db, home, cfg, tmp_path):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"))
    deleted = repo / ".claude" / "worktrees" / "gone"
    apsis = StubApsis(
        make_doc("claude", "wt", days_ago(1), project=str(tmp_path / "wt")),
        make_doc("claude", "sub", days_ago(2), project=str(repo / "sub")),
        make_doc("claude", "gone", days_ago(3), project=str(deleted)),
    )
    projects = {
        s.id: s.attribution.project for s in select(db, apsis, cfg, home).sessions
    }
    assert projects == {"wt": str(repo.resolve()), "sub": str(repo), "gone": str(repo)}


def test_domains_come_from_mappings_and_unmapped_projects_are_listed(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "mapped", days_ago(1), project="/work/repo/sub"),
        make_doc("claude", "unmapped", days_ago(2), project="/work/elsewhere"),
        fixture("session-lmstudio"),
    )
    selection = select(db, apsis, cfg, home)
    domains = {s.id: s.attribution.domain for s in selection.sessions}
    assert domains == {"mapped": "software", "unmapped": None, LMSTUDIO_ID: "general"}
    assert selection.unmapped_domains == [("claude", "/work/elsewhere")]


# Interactivity (2.9)


def test_prompts_of_interactive_sessions_stay_user_entries(db, home, cfg):
    session = _only(db, StubApsis(fixture("session-claude")), cfg, home)
    assert session.attribution.interactive
    # A user entry holding only a tool result is the tool's, not the user's.
    assert _roles(session) == ["user", "assistant", "tool", "assistant"]


def test_prompts_of_a_non_interactive_origin_are_presented_as_tool(db, home, cfg):
    doc = fixture("session-claude")
    doc["session"]["origin"] = "sdk-cli"
    session = _only(db, StubApsis(doc), cfg, home)
    assert not session.attribution.interactive
    assert _roles(session) == ["tool", "assistant", "tool", "assistant"]


@pytest.mark.parametrize("name", ["session-codex-exec", "session-lmstudio"])
def test_exec_streams_and_unmatched_lmstudio_sessions_are_non_interactive(
    db, home, cfg, name
):
    session = _only(db, StubApsis(fixture(name)), cfg, home)
    assert not session.attribution.interactive
    assert "user" not in _roles(session)


def test_a_codex_session_matched_to_the_log_keeps_its_interactive_origin(db, home, cfg):
    apsis = StubApsis(fixture("session-codex-lmstudio"), fixture("session-lmstudio"))
    attribution = _only(db, apsis, cfg, home).attribution
    assert attribution.interactive
    assert attribution.origin == "codex_cli_rs"


def test_unmapped_origins_count_as_interactive_and_are_listed(db, home, cfg):
    claude = fixture("session-claude")
    claude["session"]["origin"] = "claude-new-app"
    selection = select(db, StubApsis(claude, fixture("session-copilot")), cfg, home)
    assert all(s.attribution.interactive for s in selection.sessions)
    assert selection.unmapped_origins == [("claude", "claude-new-app"), ("copilot", "")]


def test_sessions_of_earlier_dreams_are_non_interactive(db, home, cfg):
    doc = fixture("session-claude")
    doc["session"]["project"] = str(home / "probes" / "claude" / "work")
    attribution = _only(db, StubApsis(doc), cfg, home).attribution
    assert attribution.subject == "dreaming"
    assert not attribution.interactive


# Registration (14.8)


def test_registered_sessions_take_workflow_project_and_tool_authorship(db, home, cfg):
    doc = fixture("session-claude")
    (result,) = _register(
        db,
        {
            "source": "claude",
            "session_id": doc["session"]["id"],
            "workflow": "starwave",
            "project": "/work/repo",
        },
    )
    assert result["state"] == "registered"
    attribution = _only(db, StubApsis(doc), cfg, home).attribution
    assert attribution.registered
    assert not attribution.interactive
    assert (attribution.workflow, attribution.project) == ("starwave", "/work/repo")
    assert attribution.domain == "software"


def test_entries_a_registration_marks_as_quoting_the_user_stay_user_entries(
    db, home, cfg
):
    doc = fixture("session-claude")
    sid = doc["session"]["id"]
    _register(
        db, {"source": "claude", "session_id": sid, "user_quote_entries": ["u-1"]}
    )
    session = _only(db, StubApsis(doc), cfg, home)
    assert _roles(session) == ["user", "assistant", "tool", "assistant"]


def test_a_registration_reaches_every_copy_through_its_correlation_id(db, home, cfg):
    _register(
        db,
        {
            "source": "codex-exec",
            "session_id": "elsewhere",
            "correlation_id": SERVED,
            "workflow": "orbit",
        },
    )
    apsis = StubApsis(fixture("session-codex-lmstudio"), fixture("session-lmstudio"))
    attribution = _only(db, apsis, cfg, home).attribution
    assert attribution.registered
    assert attribution.workflow == "orbit"


def test_registering_again_with_the_same_details_changes_nothing(db):
    details = {"source": "claude", "session_id": "s1", "workflow": "starwave"}
    _register(db, details)
    before = db.total_changes
    (again,) = _register(db, details)
    assert again["state"] == "unchanged"
    assert db.total_changes == before
    (changed,) = _register(db, {**details, "workflow": "orbit"})
    assert changed["state"] == "updated"
    rows = db.execute("SELECT workflow FROM registrations").fetchall()
    assert [row["workflow"] for row in rows] == ["orbit"]


def test_a_registered_file_is_selected_and_read_directly(db, home, cfg, tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(make_doc("claude", "from-file", days_ago(2))))
    (result,) = _register(db, {"file": str(path), "workflow": "orbit"})
    assert result["state"] == "registered"
    session = _only(db, StubApsis(), cfg, home)
    assert (session.source, session.id) == ("file", str(path.resolve()))
    assert session.document.id == "from-file"
    assert session.attribution.harness == "claude"
    assert session.attribution.workflow == "orbit"
    assert not session.attribution.interactive


def test_a_missing_or_invalid_file_cannot_be_registered(db, tmp_path):
    with pytest.raises(DreamingError) as missing:
        _register(db, {"file": str(tmp_path / "missing.json")})
    assert missing.value.code == "not_found"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    with pytest.raises(DreamingError) as bad:
        _register(db, {"file": str(invalid)})
    assert bad.value.code == "invalid_request"
    assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "details",
    [{}, {"source": "claude"}, {"file": "x", "source": "claude", "session_id": "s"}],
)
def test_a_registration_needs_a_source_and_session_or_a_file(db, details):
    with pytest.raises(DreamingError) as excinfo:
        _register(db, details)
    assert excinfo.value.code == "invalid_request"


def test_late_registration_reclassifies_prompts_and_queues_knowledge(db):
    add_seen(db, "claude", "s1", thread_id="t1")
    add_seen(db, "claude", "s2", thread_id="t2")
    add_observation(db, "o-quote", "claude", "s1", entry_id="u-1")
    add_observation(db, "o-prompt", "claude", "s1", entry_id="u-3")
    add_observation(db, "o-tool", "claude", "s1", entry_id="u-2", kind="tool_result")
    add_observation(db, "o-other", "claude", "s2", entry_id="u-1")
    add_item(db, "k-prompt", ["o-prompt", "o-other"])
    add_item(db, "k-other", ["o-other"])
    (result,) = _register(
        db,
        {
            "source": "claude",
            "session_id": "s1",
            "workflow": "starwave",
            "user_quote_entries": ["u-1"],
        },
    )
    assert result["reassess"] == ["k-prompt"]
    kinds = dict(db.execute("SELECT evidence_id, kind FROM observations").fetchall())
    assert kinds == {
        "o-quote": "user_statement",
        "o-prompt": "tool_prompt",
        "o-tool": "tool_result",
        "o-other": "user_statement",
    }
    seen = db.execute(
        "SELECT interactive, workflow FROM sessions_seen WHERE session_id = 's1'"
    ).fetchone()
    assert (seen["interactive"], seen["workflow"]) == (0, "starwave")
    assert pending(db) == {"k-prompt": 1, "k-other": 0}


# Recording what was read


def test_recording_a_read_session_stores_its_attribution_on_every_copy(db, home, cfg):
    add_run(db, "dream-1")
    apsis = StubApsis(fixture("session-codex-lmstudio"), fixture("session-lmstudio"))
    session = _only(db, apsis, cfg, home)
    sessions.record_session(
        db, "dream-1", session, state="read", lens_versions={"prefs": 2}
    )
    db.execute("UPDATE sessions_seen SET forgotten = 1 WHERE source = 'lmstudio'")
    sessions.record_session(
        db, "dream-1", session, state="read", lens_versions={"prefs": 2}
    )
    rows = {row["source"]: row for row in db.execute("SELECT * FROM sessions_seen")}
    assert set(rows) == {"codex", "lmstudio"}
    content = rows["codex"]
    assert content["entries_read"] == len(session.document.entries)
    assert json.loads(content["lens_versions"]) == {"prefs": 2}
    assert content["harness"] == "codex"
    assert content["locality"] == "local"
    assert content["interactive"] == 1
    assert content["subject"] == "user"
    assert content["last_run"] == "dream-1"
    assert content["thread_id"] == rows["lmstudio"]["thread_id"] == SERVED
    assert rows["lmstudio"]["forgotten"] == 1  # recording never clears it


def test_a_recorded_session_is_not_selected_again_until_it_grows(db, home, cfg):
    add_run(db, "dream-1")
    doc = fixture("session-claude")
    session = _only(db, StubApsis(doc), cfg, home)
    sessions.record_session(db, "dream-1", session, state="read")
    assert select(db, StubApsis(doc), cfg, home).sessions == []
    doc["entries"].append(
        {"id": "u-3", "role": "user", "content": [{"type": "text", "text": "More."}]}
    )
    (grown,) = select(db, StubApsis(doc), cfg, home).sessions
    assert [entry.location for entry in grown.entries()] == ["u-3"]


def test_recording_a_skipped_session_keeps_what_was_read(db):
    add_run(db, "dream-1")
    add_seen(db, "claude", "s1", entries_read=5)
    sessions.record_skipped(db, "dream-1", sessions.Skipped("claude", "s1", "bad"))
    sessions.record_skipped(db, "dream-1", sessions.Skipped("claude", "s2", "worse"))
    rows = {
        row["session_id"]: (row["state"], row["reason"], row["entries_read"])
        for row in db.execute("SELECT * FROM sessions_seen")
    }
    assert rows == {"s1": ("skipped", "bad", 5), "s2": ("skipped", "worse", 0)}

"""Contract tests for apsis shared-json documents (2.1, 2.2, 2.4, 3.x).

The fixtures were recorded from the installed apsis by tests/apsis_contract.py;
`make test-live` re-runs apsis on the same synthetic inputs.
"""

import copy
import json
import os
import shutil

import pytest
from apsis_contract import FIXTURES, build_home

from dreaming import config, sessions
from dreaming.errors import DreamingError


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


SESSION_FIXTURES = {
    "claude": "session-claude",
    "codex": "session-codex",
    "codex-lmstudio": "session-codex-lmstudio",
    "codex-exec": "session-codex-exec",
    "copilot": "session-copilot",
    "kiro": "session-kiro",
    "lmstudio": "session-lmstudio",
}


def test_listing_spans_every_project_and_source():
    listing = sessions.parse_listing(_fixture("listing"))
    assert {row.source for row in listing.rows} == {
        "claude",
        "codex",
        "codex-exec",
        "copilot",
        "lmstudio",
    }
    assert all(row.id for row in listing.rows)
    for row in listing.rows:
        assert row.project is None or row.project.startswith("/")
    assert listing.warnings == []


@pytest.mark.parametrize("name", SESSION_FIXTURES)
def test_session_documents_parse_for_every_source(name):
    doc = sessions.parse_session(_fixture(SESSION_FIXTURES[name]))
    assert doc.entries
    assert {entry.role for entry in doc.entries} >= {"assistant"}
    assert all(isinstance(entry.content, list) for entry in doc.entries)
    locations = [entry.location for entry in doc.entries]
    assert len(set(locations)) == len(locations)


def test_session_source_names():
    expected = {
        "claude": "claude",
        "codex": "codex",
        "codex-lmstudio": "codex",
        "codex-exec": "codex-exec",
        "copilot": "copilot",
        "kiro": "kiro-cli",
        "lmstudio": "lmstudio",
    }
    for name, source in expected.items():
        assert sessions.parse_session(_fixture(SESSION_FIXTURES[name])).source == source


def test_provider_and_origin_are_kept_as_recorded():
    claude = sessions.parse_session(_fixture("session-claude"))
    assert claude.origin == "cli"
    assert claude.provider is None
    # apsis 4bf9776 records no model for Claude entries, so it is unknown (2.2).
    assert claude.model is None
    assert all(entry.model is None for entry in claude.entries)

    codex = sessions.parse_session(_fixture("session-codex"))
    assert (codex.provider, codex.origin) == ("openai", "codex_exec")
    assert codex.model == "example-model-a"
    assert any(entry.model == "example-model-a" for entry in codex.entries)

    served = sessions.parse_session(_fixture("session-codex-lmstudio"))
    assert (served.provider, served.origin) == ("lmstudio", "codex_cli_rs")

    lmstudio = sessions.parse_session(_fixture("session-lmstudio"))
    assert lmstudio.provider == "lmstudio"
    assert lmstudio.origin is None

    rows = {
        (r.source, r.provider) for r in sessions.parse_listing(_fixture("listing")).rows
    }
    assert ("codex", "lmstudio") in rows and ("codex", "openai") in rows


def test_lmstudio_and_codex_exec_sessions_record_a_thread_id():
    lmstudio = sessions.parse_session(_fixture("session-lmstudio"))
    exec_stream = sessions.parse_session(_fixture("session-codex-exec"))
    assert lmstudio.thread_id
    assert exec_stream.thread_id
    assert lmstudio.correlation_id == lmstudio.thread_id
    assert exec_stream.correlation_id == exec_stream.thread_id


def test_a_codex_session_served_by_lmstudio_correlates_by_its_session_id():
    # apsis records no thread_id on Codex rollouts; Codex sends its session id as
    # LM Studio's client thread id, so that id is the correlation identifier (2.4).
    served = sessions.parse_session(_fixture("session-codex-lmstudio"))
    lmstudio = sessions.parse_session(_fixture("session-lmstudio"))
    assert served.thread_id is None
    assert served.correlation_id == served.id == lmstudio.thread_id

    rows = sessions.parse_listing(_fixture("listing")).rows
    codex_row = next(
        r for r in rows if r.source == "codex" and r.provider == "lmstudio"
    )
    lmstudio_rows = [r for r in rows if r.correlation_id == codex_row.correlation_id]
    assert {r.source for r in lmstudio_rows} == {"codex", "lmstudio"}


def test_sessions_without_a_correlation_identifier():
    assert sessions.parse_session(_fixture("session-claude")).correlation_id is None
    rows = sessions.parse_listing(_fixture("listing")).rows
    singletons = [r for r in rows if r.source == "lmstudio" and not r.thread_id]
    assert singletons and all(r.correlation_id is None for r in singletons)


def test_lmstudio_entries_carry_request_attribution():
    doc = sessions.parse_session(_fixture("session-lmstudio"))
    assert doc.requests
    assert any(entry.request_id for entry in doc.entries)


@pytest.mark.parametrize("version", [2, 0, None, "1"])
@pytest.mark.parametrize("name", ["listing", "session-claude"])
def test_unknown_schema_versions_are_rejected(name, version):
    doc = _fixture(name)
    if version is None:
        del doc["schema_version"]
    else:
        doc["schema_version"] = version
    parse = sessions.parse_listing if name == "listing" else sessions.parse_session
    with pytest.raises(DreamingError) as excinfo:
        parse(doc)
    assert excinfo.value.code == "incompatible_version"


def test_the_wrong_document_kind_is_rejected():
    with pytest.raises(DreamingError):
        sessions.parse_listing(_fixture("session-claude"))
    with pytest.raises(DreamingError):
        sessions.parse_session(_fixture("listing"))


def test_additive_fields_and_unknown_block_types_are_accepted():
    doc = copy.deepcopy(_fixture("session-claude"))
    doc["future_field"] = {"x": 1}
    doc["session"]["future_metadata"] = "y"
    doc["entries"][0]["content"].append({"type": "hologram", "data": 1})
    parsed = sessions.parse_session(doc)
    assert parsed.entries[0].content[-1]["type"] == "hologram"


# The client


def test_client_lists_with_the_configured_and_dream_exec_directories(
    isolated_home, fakes, monkeypatch, tmp_path
):
    user_dir = tmp_path / "exec-from-env"
    monkeypatch.setenv("APSIS_CODEX_EXEC_DIRS", str(user_dir))
    home = config.ensure_home()
    (home / "runs").mkdir()
    (home / "config.toml").write_text(
        '[apsis]\ncodex_exec_dirs = ["/work/exec"]\nlmstudio_log_dir = "/work/lms"\n'
    )
    client = sessions.Apsis.from_config(config.load_config(), home)
    listing = client.list_sessions()
    assert listing.rows == []

    (call,) = fakes.calls("apsis")
    argv = call["argv"]
    assert argv[:4] == ["--all-projects", "--list", "-f", "shared-json"]
    exec_dirs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--codex-exec-dir"]
    assert exec_dirs == [str(user_dir), "/work/exec", str(home / "runs")]
    assert argv[argv.index("--lmstudio-log-dir") + 1] == "/work/lms"


def test_client_opens_a_session_by_source_and_id(isolated_home, fakes):
    client = sessions.Apsis()
    doc = client.session("claude", "abc")
    assert (doc.source, doc.id) == ("claude", "abc")
    (call,) = fakes.calls("apsis")
    argv = call["argv"]
    assert argv[argv.index("-s") + 1] == "claude"
    assert argv[-1] == "abc"
    assert "--all-projects" in argv and "shared-json" in argv


def test_client_reports_apsis_failures(isolated_home, fakes):
    fakes.scenario(apsis_missing=True)
    with pytest.raises(DreamingError) as excinfo:
        sessions.Apsis().session("claude", "abc")
    assert excinfo.value.code == "apsis_failed"
    assert "session not found" in excinfo.value.message


def test_client_reports_a_missing_apsis(isolated_home, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(DreamingError) as excinfo:
        sessions.Apsis().list_sessions()
    assert excinfo.value.code == "apsis_failed"


# Live variant


@pytest.mark.live
def test_live_apsis_matches_the_recorded_contract(tmp_path, monkeypatch):
    apsis = shutil.which("apsis")
    if apsis is None:
        pytest.skip("apsis is not installed")
    extra = build_home(tmp_path)
    for name in [k for k in os.environ if k.startswith("APSIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TZ", "UTC")
    client = sessions.Apsis(
        command=apsis,
        codex_exec_dirs=[extra[1]],
        lmstudio_log_dir=extra[3],
    )

    listing = client.list_sessions()
    recorded = sessions.parse_listing(_fixture("listing"))
    assert {(r.source, r.thread_id) for r in listing.rows} == {
        (r.source, r.thread_id) for r in recorded.rows
    }
    for row in listing.rows:
        doc = client.session(row.source, row.id)
        assert doc.source == row.source
        assert doc.correlation_id == row.correlation_id

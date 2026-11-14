import json
import os
import sys
from pathlib import Path

import pytest

from dreaming.sessions import ApsisClient, Listing, SessionDocument, SessionError, parse_document

FIXTURES = Path(__file__).parent / "fixtures" / "apsis"


@pytest.mark.parametrize(
    "name",
    [
        "claude",
        "codex",
        "codex-exec",
        "copilot",
        "kiro-cli",
        "kiro-ide-chat",
        "kiro-ide-actions",
        "lmstudio",
    ],
)
def test_recorded_shared_json_contract(name):
    doc = parse_document((FIXTURES / f"{name}.json").read_text())
    assert isinstance(doc, SessionDocument)
    assert doc.schema_version == 1
    assert doc.entries
    assert all(entry.role and entry.content for entry in doc.entries)


def test_listing_across_projects():
    doc = parse_document((FIXTURES / "listing-all-projects.json").read_text())
    assert isinstance(doc, Listing)
    assert doc.scope == {"all_projects": True}
    assert all(row.source and row.id for row in doc.sessions)


def test_provider_origin_and_per_entry_models_preserved():
    doc = parse_document((FIXTURES / "codex.json").read_text())
    assert doc.metadata.provider == "example-provider"
    assert doc.metadata.origin == "codex_exec"
    assert doc.entries[-1].model == "example-model-b"
    assert doc.metadata.thread_id is None  # Source did not record one; do not invent it.


def test_thread_ids_and_missing_content_markers():
    exec_doc = parse_document((FIXTURES / "exec-session.json").read_text())
    lm_doc = parse_document((FIXTURES / "lmstudio-session-view.json").read_text())
    assert exec_doc.metadata.thread_id.startswith("0199")
    assert lm_doc.metadata.thread_id == "thread-alpha"
    assert lm_doc.entries[-1].content[0].data["truncated"] is True
    assert lm_doc.unattributed_summary
    assert lm_doc.requests[-1]["body_state"] == "incomplete"


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_incompatible_schema_versions_rejected(version):
    with pytest.raises(SessionError, match="incompatible_version"):
        parse_document(json.dumps({"schema_version": version, "kind": "session"}))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"schema_version":1,"kind":"unknown"}',
        '{"schema_version":1,"kind":"session"}',
    ],
)
def test_malformed_document(raw):
    with pytest.raises(SessionError):
        parse_document(raw)


def test_additive_fields_and_blocks_are_accepted():
    doc = json.loads((FIXTURES / "claude.json").read_text())
    doc["future_field"] = {"anything": True}
    doc["entries"][0]["content"].append({"type": "future_block", "data": 42})
    assert parse_document(json.dumps(doc)).entries[0].content[-1].type == "future_block"


@pytest.fixture
def fake_apsis(tmp_path, monkeypatch):
    executable = tmp_path / "apsis"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import os,sys,json
from pathlib import Path
args=sys.argv[1:]
Path(os.environ['APSIS_ARGS']).write_text(json.dumps(args))
if os.environ.get('APSIS_FAIL'):
    print('fake failure',file=sys.stderr); sys.exit(1)
name = 'listing-all-projects' if '--list' in args else 'codex'
print((Path(os.environ['APSIS_FIXTURES'])/(name+'.json')).read_text())
"""
    )
    executable.chmod(0o755)
    args = tmp_path / "args.json"
    monkeypatch.setenv("APSIS_ARGS", str(args))
    monkeypatch.setenv("APSIS_FIXTURES", str(FIXTURES))
    return ApsisClient(command=str(executable)), args


def test_client_uses_apsis_only_and_all_projects(fake_apsis):
    client, args = fake_apsis
    assert isinstance(client.list(), Listing)
    assert json.loads(args.read_text()) == ["--all-projects", "--list", "-f", "shared-json"]
    assert isinstance(client.open("my-session", "codex"), SessionDocument)
    assert json.loads(args.read_text()) == [
        "--all-projects",
        "-s",
        "codex",
        "-f",
        "shared-json",
        "my-session",
    ]


def test_client_preserves_configured_exec_directories(tmp_path, fake_apsis, monkeypatch):
    client, args = fake_apsis
    old = tmp_path / "old"
    new = tmp_path / "new"
    monkeypatch.setenv("APSIS_CODEX_EXEC_DIRS", str(old))
    configured = ApsisClient(command=client.command, codex_exec_dirs=(new,))
    configured.list()
    values = json.loads(args.read_text())
    assert values.count("--codex-exec-dir") == 2
    assert str(old) in values and str(new) in values


def test_client_reports_failures(fake_apsis, monkeypatch):
    client, _ = fake_apsis
    monkeypatch.setenv("APSIS_FAIL", "1")
    with pytest.raises(SessionError, match="apsis_failed"):
        client.list()


def test_no_shell_interpretation(fake_apsis):
    client, args = fake_apsis
    client.open("$(touch SHOULD_NOT_EXIST)", "custom-source")
    assert json.loads(args.read_text())[-1] == "$(touch SHOULD_NOT_EXIST)"


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DREAMING_LIVE") != "1", reason="Run make test-live")
def test_installed_apsis_contract():
    import shutil

    if not shutil.which("apsis"):
        pytest.skip("apsis is unavailable")
    client = ApsisClient()
    listing = client.list()
    if not listing.sessions:
        pytest.skip("No recorded sessions")
    row = listing.sessions[-1]
    assert isinstance(client.open(row.id, row.source), SessionDocument)

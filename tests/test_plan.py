import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from dreaming.cli import main
from dreaming.config import load_config
from dreaming.plan import dry_run, render_plan
from dreaming.sessions import ContentBlock, Entry, Listing, SessionDocument, SessionMetadata
from dreaming.steps import Step, Steps
from dreaming.store import Store


def files(home):
    return (
        {
            str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in home.rglob("*")
            if p.is_file()
        }
        if home.exists()
        else {}
    )


class Source:
    def __init__(self):
        self.metadata = SessionMetadata(source="claude", id="s", created_at="2026-11-10T12:00:00Z")

    def list(self):
        return Listing({}, (self.metadata,), ())

    def open(self, sid, source):
        return SessionDocument(
            self.metadata, (Entry("user", (ContentBlock("text", {"text": "hi"}),)),), ()
        )


@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_creates_nothing_calls_no_model_and_lists_selection(
    tmp_path, monkeypatch, existing
):
    home = tmp_path / "home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    backend = tmp_path / "backend"
    backend.write_text('#!/bin/sh\n[ "$1" = "--version" ] || exit 99\necho test-version\n')
    backend.chmod(0o755)
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        backends={"fake": replace(config.backends["claude"], command=str(backend))},
        roles={r: ("fake",) for r in config.roles},
    )
    if existing:
        with Store(home) as store:
            store.connection.execute("INSERT INTO runs(id,status) VALUES ('old','complete')")
    before = files(home)
    result = dry_run(config, home, client=Source(), now=datetime(2026, 11, 15, tzinfo=UTC))
    assert files(home) == before
    assert home.exists() == existing
    assert result["sessions"][0]["id"] == "s"
    assert result["budget"]["max_tokens"] == config.budget.max_tokens
    assert result["backends"]["fake"]["state"] == "unprobed"
    assert result["roles"]["reader"]["choices"] == ["fake"]


def test_plan_roundtrips_through_real_rune_and_never_controls_execution(tmp_path):
    if not shutil.which("rune"):
        pytest.skip("rune not installed")
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        steps = Steps(store, "r")
        steps.add(
            Step("read:<unsafe>\t<!-- comment -->", "Read"),
            Step("review", "Review", ("read:<unsafe>\t<!-- comment -->",)),
        )
        path = render_plan(store, "r")
        result = subprocess.run(
            ["rune", "list", str(path), "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout)["Stats"]["Pending"] == 2
        assert "<unsafe>" not in path.read_text()
        path.write_text("arbitrary user edit")
        assert steps.next().phase == "Read"
        assert render_plan(store, "r") == path
        assert "arbitrary" not in path.read_text()


def test_readonly_snapshot_sees_uncheckpointed_wal_without_changing_it(tmp_path):
    home = tmp_path / "home"
    with Store(home) as live:
        live.connection.execute("INSERT INTO runs(id,status) VALUES ('live','running')")
        before = files(home)
        with Store.readonly(home) as snapshot:
            assert snapshot.connection.execute("SELECT id FROM runs").fetchone() == ("live",)
        assert files(home) == before


def test_status_plan_and_missing_status_do_not_create_store(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    assert main(["status"]) == 0
    assert not home.exists()
    with Store(home) as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','waiting')")
        Steps(store, "r").add(Step("read:1", "Read"))
    assert main(["status", "--plan"]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "waiting"
    assert (home / "runs" / "r" / "tasks-dream.md").exists()


def test_cli_run_dry_run_uses_committed_lenses_without_snapshot(
    tmp_path, instruction_repo, monkeypatch, capsys
):
    from dreaming.sessions import ApsisClient

    repo, _ = instruction_repo
    path = tmp_path / "config.toml"
    path.write_text(f'[instructions]\nrepository="{repo}"\n[backends.claude]\ncommand="/missing"\n')
    home = tmp_path / "home"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    monkeypatch.setattr(ApsisClient, "list", lambda self: Source().list())
    monkeypatch.setattr(ApsisClient, "open", lambda self, sid, source: Source().open(sid, source))
    assert main(["--config", str(path), "run", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backends"]["claude"]["state"] == "unavailable"
    assert not home.exists()

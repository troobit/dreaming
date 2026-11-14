import json
import shutil
from dataclasses import replace

import pytest
from test_dispatcher import FakeBackend
from test_observe import observation
from test_plan import files
from test_reader import INSTRUCTIONS
from test_reader import env as reader_fixture
from test_selection import Source, document

from dreaming.backends.probe import ProbeResult
from dreaming.cli import main
from dreaming.config import BackendConfig, load_config
from dreaming.steps import CallResult
from dreaming.store import Store
from dreaming.trial import run_trial, trial_reader

reader_env = reader_fixture


def test_reader_trial_reports_rates_usage_and_never_writes(reader_env):
    store, config, roles, selected = reader_env
    backend = FakeBackend("fake", [CallResult(json.dumps({"observations": [observation()]}), 7, 3)])
    before = store.connection.total_changes
    result = trial_reader(store, config, roles, (selected,), backend)
    assert result["schema_valid_rate"] == 1
    assert result["quote_match_rate"] == 1
    assert result["tokens"] == 10
    assert store.connection.total_changes == before
    assert not store.connection.execute("SELECT * FROM calls").fetchall()


def test_trial_counts_invalid_schema_mismatched_quotes_and_failed_calls(reader_env):
    store, config, roles, selected = reader_env
    bad = observation()
    bad["quote"] = "invented"
    backend = FakeBackend(
        "fake",
        [
            CallResult(json.dumps({"observations": [bad]})),
            CallResult("{}"),
            CallResult("", outcome="transport_failure"),
        ],
    )
    result = trial_reader(store, config, roles, (selected, selected, selected), backend)
    assert result["schema_valid_rate"] == 1 / 3
    assert result["quote_match_rate"] == 0
    assert result["calls"] == 3


def test_cli_trial_uses_selected_backend_and_n_sessions(tmp_path, monkeypatch, capsys):
    import dreaming.trial

    seen = {}

    def trial(config, home, **options):
        seen.update(options)
        return {"schema_valid_rate": 1, "quote_match_rate": 1, "tokens": 4}

    monkeypatch.setattr(dreaming.trial, "run_trial", trial)
    monkeypatch.setenv("DREAMING_HOME", str(tmp_path / "home"))
    assert main(["trial", "reader", "--backend", "local", "--sessions", "10"]) == 0
    assert seen == {"backend_name": "local", "max_sessions": 10}
    assert json.loads(capsys.readouterr().out)["tokens"] == 4


@pytest.mark.parametrize("existing", [False, True])
def test_trial_uses_committed_instructions_without_creating_or_mutating_home(
    tmp_path,
    instruction_repo,
    monkeypatch,
    existing,
):
    repo, git = instruction_repo
    shutil.copytree(INSTRUCTIONS, repo / "instructions", dirs_exist_ok=True)
    git("add", ".")
    git("commit", "-m", "Complete instructions")
    config = replace(
        load_config(tmp_path / "config.toml"),
        instruction_repo=repo,
        backends={"fake": BackendConfig(harness="lmstudio", context_tokens=12000)},
    )
    home = tmp_path / "home"
    if existing:
        with Store(home):
            pass
    before = files(home)

    class LocalBackend(FakeBackend):
        context_tokens = 12000

        def __init__(self, name, config, run_id):
            raw = observation()
            raw["quote"] = "message 0"
            super().__init__(name, [CallResult(json.dumps({"observations": [raw]}), 5, 5)])

        def probe(self):
            return ProbeResult("fake", "lmstudio", compatible=True)

    monkeypatch.setattr("dreaming.backends.lmstudio.LMStudioBackend", LocalBackend)
    result = run_trial(
        config, home, backend_name="fake", max_sessions=1, client=Source(document("s", count=1))
    )
    assert result["sessions"] == 1 and result["quote_match_rate"] == 1
    assert files(home) == before and home.exists() == existing

"""Shared fixtures for dreaming tests."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

FAKES = Path(__file__).parent / "fakes"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point HOME and DREAMING_HOME at temporary directories outside any repository."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DREAMING_HOME", str(home / ".dreaming"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


@dataclass
class Fakes:
    """Fake `claude`, `codex` and `apsis` executables on PATH, driven by a scenario."""

    bin: Path
    scenario_path: Path
    log_path: Path

    def scenario(self, **values) -> None:
        self.scenario_path.write_text(json.dumps(values))

    def calls(self, tool: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        calls = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [call for call in calls if tool is None or call["tool"] == tool]


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for source in FAKES.iterdir():
        target = bin_dir / source.name
        body = source.read_text().split("\n", 1)[1]
        target.write_text(f"#!{sys.executable}\n{body}")
        target.chmod(0o755)
    result = Fakes(bin_dir, tmp_path / "scenario.json", tmp_path / "calls.jsonl")
    result.scenario()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SCENARIO", str(result.scenario_path))
    monkeypatch.setenv("FAKE_LOG", str(result.log_path))
    return result

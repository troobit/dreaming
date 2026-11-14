"""Opt-in checks of the design's assumptions against the installed tools.

Run with `make test-live`. Each test makes one or two small model calls with the
user's real login, and skips itself when its tool, login or server is absent.
They assert what the design relies on (Risks and Assumptions): no ~/.claude or
~/.codex instructions reach the call, no agent-launch tool is reachable unless
the call can be killed on one, apsis opens the call's session, and the
structured-output option works.
"""

import os
import shutil

import pytest

from dreaming import config
from dreaming.backends import probe

pytestmark = pytest.mark.live

REAL_HOME = os.environ["HOME"]
LMSTUDIO_URL = "http://localhost:1234"


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """The user's real HOME (for logins) with a fresh DREAMING_HOME."""
    monkeypatch.setenv("HOME", REAL_HOME)
    for name in ("FAKE_SCENARIO", "FAKE_LOG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DREAMING_HOME", str(tmp_path / "dreaming-home"))
    if shutil.which("apsis") is None:
        pytest.skip("apsis is not installed")
    return config.ensure_home()


def _probe(home, text, name):
    (home / "config.toml").write_text(text)
    result = probe.probe_backend(home, config.load_config(home), name)
    if result.status == "unavailable":
        pytest.skip(f"{name} unavailable: {result.reason}")
    return result


def _assert_design_assumptions(result):
    assert "instructions" not in result.leaks, result.reason
    assert "skills" not in result.leaks, result.reason
    assert "mcp" not in result.leaks, result.reason
    assert result.agent_enforcement in ("removed", "kill_on_agent_event"), result.reason
    assert result.apsis_finds_session is True, result.reason
    assert result.structured_output is True, result.reason
    assert result.usable, result.reason


def test_live_claude(live_home):
    if shutil.which("claude") is None:
        pytest.skip("claude is not installed")
    result = _probe(live_home, "", "claude")
    assert result.agent_tool_removable is True, result.reason
    _assert_design_assumptions(result)


def test_live_codex(live_home):
    if shutil.which("codex") is None:
        pytest.skip("codex is not installed")
    _assert_design_assumptions(_probe(live_home, "", "codex"))


def _loaded_lmstudio_model():
    try:
        models = probe.lmstudio.list_models(LMSTUDIO_URL, timeout=2)
    except probe.lmstudio.LMStudioError:
        return None
    return models[0] if models else None


def test_live_lmstudio_direct(live_home):
    model = _loaded_lmstudio_model()
    if model is None:
        pytest.skip(f"no LM Studio model loaded at {LMSTUDIO_URL}")
    text = (
        f'[backends.direct]\nharness = "lmstudio"\nmodel = "{model}"\n'
        f'base_url = "{LMSTUDIO_URL}"\ncontext_tokens = 8192\n'
    )
    _assert_design_assumptions(_probe(live_home, text, "direct"))


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_live_harness_on_lmstudio(live_home, harness):
    if shutil.which(harness) is None:
        pytest.skip(f"{harness} is not installed")
    model = _loaded_lmstudio_model()
    if model is None:
        pytest.skip(f"no LM Studio model loaded at {LMSTUDIO_URL}")
    text = (
        f'[backends.local]\nharness = "{harness}"\nprovider = "lmstudio"\n'
        f'model = "{model}"\nbase_url = "{LMSTUDIO_URL}"\ncontext_tokens = 8192\n'
    )
    _assert_design_assumptions(_probe(live_home, text, "local"))

"""Explicit opt-in only: these tests make real, budgeted model calls."""

import os
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

from dreaming.backends.probe import probe_backend
from dreaming.config import BackendConfig

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("DREAMING_LIVE") != "1", reason="Run make test-live to enable model calls"
    ),
]


@pytest.mark.parametrize("harness", ["claude", "codex", "lmstudio"])
def test_live_harness_isolation_and_visibility(tmp_path, harness):
    command = "codex" if harness == "lmstudio" else harness
    if not shutil.which(command) or not shutil.which("apsis"):
        pytest.skip("Harness or apsis is not installed")
    if harness == "lmstudio":
        model = os.environ.get("DREAMING_LIVE_LMSTUDIO_MODEL")
        if not model:
            pytest.skip("Set DREAMING_LIVE_LMSTUDIO_MODEL to a loaded model")
        try:
            with urllib.request.urlopen("http://localhost:1234/v1/models", timeout=2):
                pass
        except (OSError, urllib.error.URLError):
            pytest.skip("LM Studio server is not running")
    else:
        model = None
        auth_args = ["auth", "status"] if harness == "claude" else ["login", "status"]
        status = subprocess.run([command, *auth_args], capture_output=True, timeout=10)
        if status.returncode:
            pytest.skip("Harness is not logged in")
    result = probe_backend(
        harness,
        BackendConfig(
            harness=command,
            command=command,
            model=model,
            provider="lmstudio" if harness == "lmstudio" else None,
        ),
        tmp_path / "private",
        max_tokens=20000,
    )
    assert result.compatible, result.reason
    assert not result.instruction_leaks and not result.skill_leaks
    assert result.apsis_visible
    assert not result.agent_tools or result.enforcement_required
    assert result.structured_output

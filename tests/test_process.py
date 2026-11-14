import json
import os
import sys
import time

import pytest

from dreaming.backends.process import run_process


@pytest.mark.parametrize("newline", [True, False])
def test_agent_events_rejected_even_at_eof(tmp_path, newline):
    event = json.dumps({"type": "item.started", "item": {"type": "collab_tool_call"}})
    result = run_process(
        [sys.executable, "-c", f"print({event!r}, end={chr(10) if newline else ''!r})"],
        tmp_path,
        dict(os.environ),
        2,
    )
    assert "agent" in result.reason


def test_timeout_after_stdout_closes(tmp_path):
    start = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import os,time; os.close(1); time.sleep(10)"],
        tmp_path,
        dict(os.environ),
        0.2,
    )
    assert result.reason == "timeout"
    assert time.monotonic() - start < 3


def test_claude_nested_agent_call_is_killed(tmp_path):
    event = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent"}]}}
    )
    result = run_process(
        [sys.executable, "-c", f"print({event!r}, flush=True); import time; time.sleep(30)"],
        tmp_path,
        dict(os.environ),
        2,
    )
    assert "agent" in result.reason


def test_large_prompt_passes_through_stdin_without_argv_limits(tmp_path):
    prompt = "é" * 300000
    result = run_process(
        [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
        tmp_path,
        dict(os.environ),
        2,
        input_text=prompt,
    )
    assert result.returncode == 0 and result.stdout.strip() == "300000"

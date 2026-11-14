import json
import sys
from dataclasses import replace

import pytest

from dreaming.backends.claude import ClaudeBackend
from dreaming.backends.codex import CodexBackend
from dreaming.backends.probe import ProbeResult
from dreaming.config import BackendConfig


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = tmp_path / "harness"
    log = tmp_path / "log"
    monkeypatch.setenv("HARNESS_LOG", str(log))
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
Path(os.environ['HARNESS_LOG']).write_text(json.dumps({'args': args, 'cwd': os.getcwd(),
    'env': {k:v for k,v in os.environ.items() if k.startswith(('CLAUDE_', 'ANTHROPIC_'))}}))
if os.environ.get('HARNESS_LAUNCH'):
    event = {'type':'item.started','item':{'type':'collab_tool_call','tool':'spawn_agent'}}
    print(json.dumps(event), flush=True)
    time.sleep(30)
elif 'exec' in args:
    print(json.dumps({'type':'thread.started','thread_id':'codex-123'}))
    Path(args[args.index('-o')+1]).write_text('{"ok":true}')
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}))
else:
    sid=args[args.index('--session-id')+1]
    print(json.dumps({'type':'result','session_id':sid,'structured_output':{'ok': True},
        'usage':{'input_tokens':12,'output_tokens':3}}))
"""
    )
    path.chmod(0o755)
    return path, log


def capabilities(harness):
    flags = (
        (
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--tools",
            "Read",
            "--permission-mode",
            "dontAsk",
        )
        if harness == "claude"
        else (
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        )
    )
    return ProbeResult(
        "test",
        harness,
        compatible=True,
        structured_output=True,
        flags=flags,
        tools=("Read", "web_search", "web_fetch", "pr_read"),
    )


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_adapters_filter_shared_definitions_and_keep_session_records(tmp_path, harness, kind):
    path, log = harness
    cls = ClaudeBackend if kind == "claude" else CodexBackend
    definitions = {
        "devtools": {
            "transport": {
                "type": "stdio",
                "command": "mcp-devtools",
                "env": {"ENABLE_ADDITIONAL_TOOLS": "web_search,codex-agent,gemini-agent"},
            }
        },
        "unrelated": {"transport": {"type": "stdio", "command": "unexpected"}},
    }
    backend = cls(
        "test",
        BackendConfig(harness=kind, command=str(path)),
        tmp_path / "private",
        "run",
        capabilities=capabilities(kind),
        servers=definitions,
    )
    sid = backend.new_session()
    result = backend.call(
        "researcher", "question", {"type": "object"}, ["Read", "web_search", "web_fetch", "pr_read"]
    )
    assert result.outcome == "success" and json.loads(result.raw_output) == {"ok": True}
    assert result.tokens_in == 12 and result.tokens_out == 3 and result.session_ref
    record = json.loads(log.read_text())
    assert str(tmp_path / "private") in record["cwd"]
    config_files = list((tmp_path / "private" / "runs" / "run").rglob("mcp.json"))
    assert config_files
    config = json.loads(config_files[0].read_text())["mcpServers"]
    assert set(config) == {"devtools"}
    assert "agent" not in config["devtools"]["env"]["ENABLE_ADDITIONAL_TOOLS"]
    assert (
        "--permission-mode" in record["args"]
        if kind == "claude"
        else 'approval_policy="never"' in record["args"]
    )
    if kind == "claude":
        assert sid == result.session_ref
        assert record["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
        tools = record["args"][record["args"].index("--tools") + 1]
        assert "Agent" not in tools
    else:
        assert result.session_ref == "codex-123"
        assert list((tmp_path / "private" / "runs" / "run" / "codex").glob("*.jsonl"))
        assert list((tmp_path / "private" / "codex-exec").glob("*.jsonl"))
        assert any("enabled_tools" in arg and "web_search" in arg for arg in record["args"])


def test_codex_agent_event_kills_call(tmp_path, harness, monkeypatch):
    path, _ = harness
    monkeypatch.setenv("HARNESS_LAUNCH", "1")
    backend = CodexBackend(
        "test",
        BackendConfig(harness="codex", command=str(path)),
        tmp_path / "private",
        "run",
        capabilities=replace(capabilities("codex"), tools=("Read",)),
    )
    result = backend.call("researcher", "hi", {}, ["Read"])
    assert result.outcome == "transport_failure" and "agent" in result.error


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_incompatible_probe_prevents_model_call(tmp_path, harness, kind):
    path, log = harness
    cls = ClaudeBackend if kind == "claude" else CodexBackend
    backend = cls(
        "test",
        BackendConfig(harness=kind, command=str(path)),
        tmp_path / "private",
        "run",
        capabilities=replace(capabilities(kind), compatible=False, reason="instruction leak"),
    )
    assert backend.call("reader", "hi", {}, []).outcome == "transport_failure"
    assert not log.exists()


def test_claude_local_provider_and_tool_free_role(tmp_path, harness):
    path, log = harness
    backend = ClaudeBackend(
        "test",
        BackendConfig(command=str(path), provider="lmstudio"),
        tmp_path / "private",
        "run",
        capabilities=capabilities("claude"),
    )
    assert backend.call("reader", "hi", {}, []).outcome == "success"
    record = json.loads(log.read_text())
    assert record["args"][record["args"].index("--tools") + 1] == ""
    assert record["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:1234"


def test_agent_tools_rejected_even_if_explicitly_requested(tmp_path, harness):
    path, log = harness
    backend = ClaudeBackend(
        "test",
        BackendConfig(command=str(path)),
        tmp_path / "private",
        "run",
        capabilities=capabilities("claude"),
    )
    with pytest.raises(ValueError, match="agent"):
        backend.call("researcher", "hi", {}, ["mcp__devtools__codex-agent"])
    assert not log.exists()

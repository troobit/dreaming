import json
import sqlite3
import sys
from dataclasses import replace

import pytest

from dreaming.backends.probe import probe_backend
from dreaming.config import BackendConfig


@pytest.fixture
def fake_harness(tmp_path, monkeypatch):
    executable = tmp_path / "harness"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["PROBE_LOG"], "a") as log:
    log.write(json.dumps({"args":args,"cwd":os.getcwd(),
        "disable_mds":os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS")}) + "\\n")
scenario = os.environ.get("PROBE_SCENARIO", "ok")
if "--version" in args:
    print(os.environ.get("PROBE_VERSION", "harness 1.0")); sys.exit(0)
if "--help" in args:
    print("--safe-mode --json-schema --output-schema --ignore-user-config"); sys.exit(0)
if scenario == "fallback" and "--safe-mode" in args:
    print("error: unknown option --safe-mode", file=sys.stderr); sys.exit(2)
if scenario == "no-schema" and ("--json-schema" in args or "--output-schema" in args):
    print("error: unknown option schema", file=sys.stderr); sys.exit(2)
if "prompt-input" in args:
    tool_names = ["Read", "spawn_agent"] if scenario == "agents" else ["Read"]
    print(json.dumps([{"type":"message","role":"system","tools":[{
        "type":"namespace","name":"functions",
        "tools":[{"type":"function","name":name} for name in tool_names]}]},
        {"type":"message","role":"user","content":[{"type":"input_text",
            "text":"<INSTRUCTIONS>leak</INSTRUCTIONS>" if scenario == "leak" else "probe"}]}]))
    sys.exit(0)
if scenario == "timeout":
    time.sleep(30); sys.exit(0)
if scenario == "launch":
    print(json.dumps({"type":"item.started", "item":{"type":"collab_tool_call",
        "tool":"spawn_agent"}}), flush=True)
    time.sleep(30); sys.exit(0)
payload = {"tools":["Read"],"instruction_files":[],"skills":[]}
if scenario == "leak": payload["instruction_files"] = ["AGENTS.md"]
if scenario == "skills": payload["skills"] = ["unexpected-skill"]
if "exec" in args:
    print(json.dumps({"type":"thread.started","thread_id":"codex-session"}))
    if "-o" in args:
        Path(args[args.index("-o")+1]).write_text(json.dumps(payload))
    print(json.dumps({"type":"item.completed",
        "item":{"type":"agent_message", "text":json.dumps(payload)}}))
    print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":7}}))
else:
    sid = args[args.index("--session-id")+1]
    print(json.dumps({"type":"system","subtype":"init","session_id":sid,"tools":["Read"]}))
    print(json.dumps({"type":"result","session_id":sid,"structured_output":payload,
        "result":json.dumps(payload),"usage":{"input_tokens":11,"output_tokens":7}}))
"""
    )
    executable.chmod(0o755)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("PROBE_LOG", str(log))

    def backend(harness="claude"):
        return BackendConfig(harness=harness, command=str(executable), timeout_seconds=2)

    return backend, log


def visible(source, session_ref, exec_dir):
    return bool(source and session_ref)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_probe_records_capabilities_and_budget(tmp_path, fake_harness, harness):
    backend, log = fake_harness
    home = tmp_path / "private"
    result = probe_backend("test", backend(harness), home, visibility=visible)
    assert result.state == "probed"
    assert result.compatible
    assert result.tools == ("Read",)
    assert result.structured_output
    assert result.apsis_visible
    assert result.tokens_in == 11 and result.tokens_out == 7
    assert result.session_ref
    with sqlite3.connect(home / "dreaming.db") as conn:
        assert conn.execute("SELECT SUM(tokens_in + tokens_out) FROM calls").fetchone()[0] == 18
        assert conn.execute("SELECT session_ref FROM calls").fetchone()[0] == result.session_ref
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "complete"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    model_calls = [c for c in calls if "--session-id" in c["args"] or "exec" in c["args"]]
    assert all(str(home) in c["cwd"] for c in model_calls)
    if harness == "claude":
        assert "--safe-mode" in result.flags
        assert model_calls[0]["disable_mds"] == "1"
    else:
        assert any("prompt-input" in c["args"] for c in calls)
        assert list((home / "runs").rglob("*.jsonl"))


def test_dry_probe_only_reads_version_and_cache(tmp_path, fake_harness, monkeypatch):
    backend, log = fake_harness
    home = tmp_path / "private"
    result = probe_backend("test", backend(), home, dry_run=True)
    assert result.state == "unprobed"
    assert not home.exists()
    assert [json.loads(line)["args"] for line in log.read_text().splitlines()] == [["--version"]]
    probe_backend("test", backend(), home, visibility=visible)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.rglob("*") if p.is_file()}
    cached = probe_backend("test", backend(), home, dry_run=True)
    assert cached.state == "cached" and cached.compatible
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.rglob("*") if p.is_file()
    }
    monkeypatch.setenv("PROBE_VERSION", "harness 2.0")
    assert probe_backend("test", backend(), home, dry_run=True).state == "unprobed"


@pytest.mark.parametrize(
    "scenario, field", [("leak", "instruction_leaks"), ("skills", "skill_leaks")]
)
def test_isolation_leaks_reject_backend(tmp_path, fake_harness, monkeypatch, scenario, field):
    backend, _ = fake_harness
    monkeypatch.setenv("PROBE_SCENARIO", scenario)
    result = probe_backend("test", backend("codex"), tmp_path / "private", visibility=visible)
    assert not result.compatible
    assert getattr(result, field)


def test_safe_mode_fallback(tmp_path, fake_harness, monkeypatch):
    backend, _ = fake_harness
    monkeypatch.setenv("PROBE_SCENARIO", "fallback")
    result = probe_backend("test", backend(), tmp_path / "private", visibility=visible)
    assert result.compatible
    assert "--setting-sources" in result.flags
    assert result.call_count == 2


def test_schema_flag_falls_back_to_program_validation(tmp_path, fake_harness, monkeypatch):
    backend, _ = fake_harness
    monkeypatch.setenv("PROBE_SCENARIO", "no-schema")
    result = probe_backend("test", backend(), tmp_path / "private", visibility=visible)
    assert result.compatible
    assert result.structured_output is False


@pytest.mark.parametrize("scenario, reason", [("launch", "agent"), ("timeout", "timeout")])
def test_process_is_killed_on_agent_event_or_timeout(
    tmp_path, fake_harness, monkeypatch, scenario, reason
):
    backend, _ = fake_harness
    monkeypatch.setenv("PROBE_SCENARIO", scenario)
    config = replace(backend("codex"), timeout_seconds=1)
    result = probe_backend("test", config, tmp_path / "private", visibility=visible)
    assert not result.compatible
    assert reason in result.reason.lower()


def test_apsis_invisibility_rejects_backend(tmp_path, fake_harness):
    backend, _ = fake_harness
    result = probe_backend("test", backend(), tmp_path / "private", visibility=lambda *args: False)
    assert not result.compatible
    assert "apsis" in result.reason


def test_no_model_call_when_probe_budget_is_exhausted(tmp_path, fake_harness):
    backend, log = fake_harness
    result = probe_backend(
        "test", backend(), tmp_path / "private", max_tokens=1, visibility=visible
    )
    assert not result.compatible
    assert "budget" in result.reason
    assert result.call_count == 0
    assert all(
        "--session-id" not in json.loads(line)["args"] for line in log.read_text().splitlines()
    )


def test_direct_lmstudio_requires_harness_probe(tmp_path):
    result = probe_backend(
        "local", BackendConfig(harness="lmstudio"), tmp_path / "private", dry_run=True
    )
    assert not result.compatible
    assert "harness" in result.reason


def test_codex_agent_tool_is_reported_with_enforcement(tmp_path, fake_harness, monkeypatch):
    backend, _ = fake_harness
    monkeypatch.setenv("PROBE_SCENARIO", "agents")
    result = probe_backend("test", backend("codex"), tmp_path / "private", visibility=visible)
    assert result.compatible
    assert result.agent_tools == ("spawn_agent",)
    assert result.agent_tool_removable is False
    assert result.enforcement_required


def test_wall_clock_budget_stops_before_model_call(tmp_path, fake_harness):
    backend, _ = fake_harness
    result = probe_backend(
        "test", backend(), tmp_path / "private", max_seconds=0, visibility=visible
    )
    assert not result.compatible and result.call_count == 0
    assert "budget" in result.reason


def test_saved_exec_stream_is_visible_to_installed_apsis(tmp_path, fake_harness):
    import shutil

    if not shutil.which("apsis"):
        pytest.skip("apsis is not installed")
    backend, _ = fake_harness
    result = probe_backend("test", backend("codex"), tmp_path / "private")
    assert result.apsis_visible, result.reason

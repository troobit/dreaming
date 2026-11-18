"""Backend probes for isolation and structured output (1.7, 4.4, 5.5, 5.7, 5.8)."""

import json

import pytest
from lmstudio_fake import FakeLMStudio, model

from dreaming import cli, config
from dreaming.backends import probe


def _config(isolated_home, text=""):
    home = isolated_home / ".dreaming"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text)
    return config.ensure_home(), config.load_config()


@pytest.fixture
def setup(isolated_home, fakes):
    return _config(isolated_home)


def _model_calls(fakes, tool):
    return [c for c in fakes.calls(tool) if c["argv"] != ["--version"]]


# Claude


def test_clean_claude_probe(setup, fakes):
    home, cfg = setup
    result = probe.probe_backend(home, cfg, "claude")

    assert result.status == "probed"
    assert result.version == "2.1.311 (Claude Code)"
    assert result.isolation == "safe-mode"
    assert result.leaks == []
    assert "Agent" not in result.tools and "Task" not in result.tools
    assert result.agent_tool_removable is True
    assert result.structured_output is True
    assert result.apsis_finds_session is True
    assert result.usable
    assert result.tokens_in == 125 and result.tokens_out == 30

    (call,) = _model_calls(fakes, "claude")
    argv = call["argv"]
    assert "--safe-mode" in argv
    assert argv[argv.index("--session-id") + 1] == result.session_ref
    assert "--json-schema" in argv
    assert call["cwd"].startswith(str(home))
    (lookup,) = fakes.calls("apsis")
    assert result.session_ref in lookup["argv"]
    assert lookup["argv"][lookup["argv"].index("-s") + 1] == "claude"


def test_claude_falls_back_to_setting_sources_when_safe_mode_is_unknown(setup, fakes):
    home, cfg = setup
    fakes.scenario(unknown_flags=["--safe-mode"])
    result = probe.probe_backend(home, cfg, "claude")

    assert result.isolation == "setting-sources"
    assert result.usable
    last = _model_calls(fakes, "claude")[-1]
    assert last["argv"][last["argv"].index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in last["argv"]
    assert "--disable-slash-commands" in last["argv"]
    assert last["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert last["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_claude_instruction_leak_is_detected_with_a_canary(setup, fakes):
    home, cfg = setup
    fakes.scenario(leak_instructions=True)
    result = probe.probe_backend(home, cfg, "claude")

    assert "instructions" in result.leaks
    assert not result.usable
    assert "instructions" in result.reason
    # Both isolation sets were tried before giving up.
    assert len(_model_calls(fakes, "claude")) == 2


def test_claude_skill_mcp_and_agent_leaks(setup, fakes):
    home, cfg = setup
    fakes.scenario(extra_tools=["Agent"], skills=["review"], mcp_servers=["devtools"])
    result = probe.probe_backend(home, cfg, "claude")

    assert set(result.leaks) >= {"agent_tool", "skills", "mcp"}
    assert result.agent_tool_removable is False
    assert not result.usable


def test_claude_without_structured_output_is_recorded_but_usable(setup, fakes):
    home, cfg = setup
    fakes.scenario(structured=False)
    result = probe.probe_backend(home, cfg, "claude")
    assert result.structured_output is False
    assert result.usable


def test_apsis_not_finding_the_session_makes_the_backend_unusable(setup, fakes):
    home, cfg = setup
    fakes.scenario(apsis_missing=True)
    result = probe.probe_backend(home, cfg, "claude")
    assert result.apsis_finds_session is False
    assert not result.usable
    assert "apsis" in result.reason


def test_a_failing_call_is_unavailable_with_the_error(setup, fakes):
    home, cfg = setup
    fakes.scenario(fail="Invalid API key")
    result = probe.probe_backend(home, cfg, "claude")
    assert result.status == "unavailable"
    assert "Invalid API key" in result.reason
    assert not result.usable


def test_a_missing_executable_is_unavailable(setup, fakes, monkeypatch):
    home, cfg = setup
    monkeypatch.setenv("PATH", "/nonexistent")
    result = probe.probe_backend(home, cfg, "claude")
    assert result.status == "unavailable"
    assert "claude" in result.reason


def test_claude_backend_on_lmstudio_points_the_harness_at_the_server(
    isolated_home, fakes
):
    home, cfg = _config(
        isolated_home,
        """
[backends.claude-local]
harness = "claude"
provider = "lmstudio"
model = "qwen3-coder"
base_url = "http://localhost:1234"
context_tokens = 32768
""",
    )
    probe.probe_backend(home, cfg, "claude-local")
    (call,) = _model_calls(fakes, "claude")
    assert call["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:1234"
    assert call["argv"][call["argv"].index("--model") + 1] == "qwen3-coder"


# Codex


def test_codex_prefers_the_user_home_when_it_is_clean(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    result = probe.probe_backend(home, cfg, "codex")

    assert result.status == "probed"
    assert result.version == "codex-cli 0.169.0"
    assert result.isolation == "user-home"
    assert result.leaks == []
    assert result.usable
    assert result.tokens_in == 200 and result.tokens_out == 40

    views = [
        c for c in fakes.calls("codex") if c["argv"][:2] == ["debug", "prompt-input"]
    ]
    assert views and "skills.include_instructions=false" in views[0]["argv"]
    (run,) = [c for c in fakes.calls("codex") if c["argv"][:1] == ["exec"]]
    assert "--json" in run["argv"] and "--output-schema" in run["argv"]
    assert run["cwd"].startswith(str(home))
    stream = home / "probes" / "codex" / "codex" / f"{result.session_ref}.jsonl"
    assert stream.exists()
    listing = [c for c in fakes.calls("apsis") if "--list" in c["argv"]][-1]
    assert str(stream.parent) in listing["argv"]


def test_codex_moves_to_an_isolated_home_when_the_user_home_leaks(isolated_home, fakes):
    codex_home = isolated_home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    home, cfg = _config(isolated_home)
    fakes.scenario(leak_with_user_home=True)
    result = probe.probe_backend(home, cfg, "codex")

    assert result.isolation == "isolated-home"
    assert result.leaks == []
    assert result.usable
    (run,) = [c for c in fakes.calls("codex") if c["argv"][:1] == ["exec"]]
    assert run["codex_home"] == str(home / "codex-home")
    assert (home / "codex-home" / "auth.json").resolve() == codex_home / "auth.json"


def test_codex_isolated_home_that_replaces_the_login_link_is_refused(
    isolated_home, fakes
):
    codex_home = isolated_home / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    home, cfg = _config(isolated_home)
    fakes.scenario(leak_with_user_home=True, replace_auth_link=True)
    result = probe.probe_backend(home, cfg, "codex")
    assert not result.usable
    assert "replaced the auth.json link" in result.reason


def test_codex_isolated_home_that_loses_the_login_is_reported(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    fakes.scenario(leak_with_user_home=True, fail_with_codex_home=True)
    result = probe.probe_backend(home, cfg, "codex")
    assert not result.usable
    assert "not logged in" in result.reason
    assert "user-home" in result.reason


def test_codex_skills_that_cannot_be_removed_block_the_backend(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    fakes.scenario(skills_always=True)
    result = probe.probe_backend(home, cfg, "codex")
    assert "skills" in result.leaks
    assert not result.usable
    assert not [c for c in fakes.calls("codex") if c["argv"][:1] == ["exec"]]


def test_codex_spawn_agent_is_enforced_by_killing_the_call(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    fakes.scenario(agent_tool=True)
    result = probe.probe_backend(home, cfg, "codex")
    assert "agent_tool" in result.leaks
    assert result.agent_tool_removable is False
    assert result.agent_enforcement == "kill_on_agent_event"
    assert result.usable


def test_codex_instruction_canary_is_detected(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    fakes.scenario(leak_instructions=True)
    result = probe.probe_backend(home, cfg, "codex")
    assert "instructions" in result.leaks
    assert not result.usable


def test_codex_on_lmstudio_uses_the_oss_provider(isolated_home, fakes):
    home, cfg = _config(
        isolated_home,
        """
[backends.codex-local]
harness = "codex"
provider = "lmstudio"
model = "qwen3-coder"
context_tokens = 32768
""",
    )
    probe.probe_backend(home, cfg, "codex-local")
    # `codex debug prompt-input` takes no provider options; the call does.
    (call,) = [c for c in fakes.calls("codex") if c["argv"][:1] == ["exec"]]
    for argv in [call["argv"]]:
        assert "--oss" in argv
        assert argv[argv.index("--local-provider") + 1] == "lmstudio"
        assert argv[argv.index("-m") + 1] == "qwen3-coder"


# LM Studio direct


@pytest.fixture
def lmstudio(tmp_path, fakes):
    server = FakeLMStudio(thread_log=str(tmp_path / "lmstudio-threads")).start()
    fakes.scenario(lmstudio_log=server.thread_log)
    yield server
    server.stop()


def _lmstudio_config(isolated_home, url, model_id="qwen3"):
    return _config(
        isolated_home,
        f"""
[backends.direct]
harness = "lmstudio"
model = "{model_id}"
base_url = "{url}"
context_tokens = 32768
""",
    )


def test_direct_lmstudio_probe(isolated_home, lmstudio):
    home, cfg = _lmstudio_config(isolated_home, lmstudio.url)
    result = probe.probe_backend(home, cfg, "direct")

    assert result.status == "probed"
    assert result.isolation == "direct"
    assert result.tools == [] and result.leaks == []
    assert result.structured_output is True
    assert result.apsis_finds_session is True
    assert result.usable
    assert result.tokens_in == 12 and result.tokens_out == 4
    (body,) = lmstudio.bodies
    # The thread id leads the body so a truncated log entry still carries it (5.8).
    assert body.startswith(b'{"client_metadata": {"thread_id": "')
    assert json.loads(body)["client_metadata"]["thread_id"] == result.session_ref


def test_direct_lmstudio_without_the_model_loaded_is_unavailable(
    isolated_home, lmstudio
):
    home, cfg = _lmstudio_config(isolated_home, lmstudio.url, model_id="missing-model")
    result = probe.probe_backend(home, cfg, "direct")
    assert result.status == "unavailable"
    assert "missing-model" in result.reason
    assert lmstudio.bodies == []


def test_a_downloaded_model_that_is_not_loaded_is_unavailable(isolated_home, lmstudio):
    """LM Studio would load it on request, and dreaming never loads a model."""
    lmstudio.models = [model("qwen3", state="not-loaded")]
    home, cfg = _lmstudio_config(isolated_home, lmstudio.url)
    result = probe.probe_backend(home, cfg, "direct")
    assert result.status == "unavailable"
    assert lmstudio.bodies == []


def test_unreachable_lmstudio_is_unavailable(isolated_home, fakes):
    home, cfg = _lmstudio_config(isolated_home, "http://127.0.0.1:9")
    result = probe.probe_backend(home, cfg, "direct")
    assert result.status == "unavailable"
    assert not result.usable


# Cache and dry run


def test_results_are_cached_by_backend_version(setup, fakes):
    home, cfg = setup
    probed = probe.probe_backend(home, cfg, "claude")
    calls_before = len(fakes.calls())

    cached = probe.cached_status(home, cfg, "claude")
    assert cached.status == "cached"
    assert cached.leaks == probed.leaks and cached.tools == probed.tools
    assert cached.usable
    # Only `--version` ran: no model call and no apsis lookup.
    new_calls = fakes.calls()[calls_before:]
    assert [c["argv"] for c in new_calls] == [["--version"]]


def test_a_new_backend_version_is_unprobed(setup, fakes):
    home, cfg = setup
    probe.probe_backend(home, cfg, "claude")
    fakes.scenario(version="2.2.0 (Claude Code)")
    status = probe.cached_status(home, cfg, "claude")
    assert status.status == "unprobed"
    assert status.version == "2.2.0 (Claude Code)"
    assert not status.usable


def test_a_changed_backend_config_is_unprobed(isolated_home, fakes):
    home, cfg = _config(isolated_home)
    probe.probe_backend(home, cfg, "claude")
    _, changed = _config(
        isolated_home,
        '[backends.claude]\nharness = "claude"\n'
        'model = "opus"\ncontext_tokens = 200000\n',
    )
    assert probe.cached_status(home, changed, "claude").status == "unprobed"


def test_dry_run_status_changes_no_state(setup, fakes):
    home, cfg = setup
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    status = probe.cached_status(home, cfg, "codex")
    assert status.status == "unprobed"
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before
    assert all(c["argv"] == ["--version"] for c in fakes.calls())


def test_dry_run_of_an_unavailable_backend(setup, fakes, monkeypatch):
    home, cfg = setup
    monkeypatch.setenv("PATH", "/nonexistent")
    status = probe.cached_status(home, cfg, "claude")
    assert status.status == "unavailable"


# The command


def test_dream_probe_probes_every_backend_and_writes_the_cache(setup, fakes, capsys):
    home, _ = setup
    assert cli.main(["probe", "--json"]) == 0
    results = {r["backend"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(results) == {"claude", "codex"}
    assert results["claude"]["status"] == "probed"
    assert (home / "probes" / "claude.json").exists()

    assert cli.main(["probe", "--cached", "claude"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "cached" in out


def test_dream_probe_rejects_an_unknown_backend(setup, fakes, capsys):
    assert cli.main(["probe", "nowhere"]) == 2
    assert "nowhere" in capsys.readouterr().err

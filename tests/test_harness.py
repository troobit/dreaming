"""Claude Code and Codex as role harnesses, with fake executables (4.4, 4.6, 5.1,
5.7, 5.8)."""

import json
import time
import tomllib

import pytest
from runkit import CallPhase, FakeClock, add_run, reads

from dreaming import config, run
from dreaming.backends import BackendError, claude, codex, lmstudio, probe
from dreaming.errors import USAGE_ERROR, DreamingError

DEVTOOLS_EXTRA = ("sequential_thinking", "gemini-agent", "codex-agent")
SERVERS = {
    "devtools": {
        "transport": {
            "type": "stdio",
            "command": "mcp-devtools",
            "env": {"ENABLE_ADDITIONAL_TOOLS": ",".join(DEVTOOLS_EXTRA)},
        },
        "secrets": {},
        "surfaces": ["claude", "codex"],
    },
    "github": {
        "transport": {"type": "http", "url": "https://api.githubcopilot.com/mcp/"},
        "secrets": {"AUTH_TOKEN": {"header": "Authorization"}},
    },
    "svelte": {
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@sveltejs/mcp"],
        },
    },
}
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
RESEARCH = ["read", "grep", "glob", "devtools.internet_search", "devtools.fetch_url"]
PR_THREADS = "github.pull_request_read"
RUN = "dream-test"


@pytest.fixture
def make_cfg(tmp_path, home):
    servers = tmp_path / "servers.json"
    servers.write_text(json.dumps(SERVERS))
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    target = tmp_path / "target-repo"
    target.mkdir()

    def make(**sections):
        raw = {
            "mcp": {"servers_file": str(servers)},
            "knowledge_folders": [str(knowledge), str(tmp_path / "missing-folder")],
            "repositories": {"target": {"path": str(target)}},
            "backends": {
                "claude-local": {
                    "harness": "claude",
                    "provider": "lmstudio",
                    "model": "qwen3-coder",
                    "context_tokens": 32_768,
                },
                "codex-local": {
                    "harness": "codex",
                    "provider": "lmstudio",
                    "model": "qwen3-coder",
                    "context_tokens": 32_768,
                },
            },
        }
        raw.update(sections)
        return config.build_config(raw)

    return make


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


def flag(argv, name):
    return argv[argv.index(name) + 1]


def only_call(fakes, tool):
    (call,) = [c for c in fakes.calls(tool) if c["argv"] != ["--version"]]
    return call


def claude_backend(cfg, home, name="claude", **kwargs):
    return claude.ClaudeBackend(cfg, name, home=home, run_id=RUN, **kwargs)


def codex_backend(cfg, home, name="codex", **kwargs):
    return codex.CodexBackend(cfg, name, home=home, run_id=RUN, **kwargs)


# Claude Code


def test_claude_call_returns_the_output_usage_and_session(cfg, home, fakes):
    fakes.scenario(answer={"value": 3})
    result = claude_backend(cfg, home).call(
        "reader", "read this", SCHEMA, [], session_id="s-1"
    )
    assert result.output == {"value": 3}
    assert (result.tokens_in, result.tokens_out) == (125, 30)
    assert result.session_ref == "s-1"
    assert result.model == "claude-test"
    call = only_call(fakes, "claude")
    assert call["stdin"] == "read this"
    assert flag(call["argv"], "--session-id") == "s-1"
    assert json.loads(flag(call["argv"], "--json-schema")) == SCHEMA
    # The working directory is under the home, so no parent-directory
    # instruction file loads and the session counts as the dream's own (1.5).
    assert call["cwd"].startswith(str(home / "runs" / RUN))


def test_claude_uses_the_isolation_the_probe_confirmed(cfg, home, fakes):
    claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    claude_backend(cfg, home, isolation="setting-sources").call(
        "reader", "p", SCHEMA, []
    )
    safe, sources = [c for c in fakes.calls("claude")]
    assert "--safe-mode" in safe["argv"]
    assert "--setting-sources" not in safe["argv"]
    assert flag(sources["argv"], "--setting-sources") == ""
    assert "--disable-slash-commands" in sources["argv"]
    assert sources["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert sources["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    for call in (safe, sources):
        assert "--strict-mcp-config" in call["argv"]


def test_claude_gets_an_explicit_tool_list_without_agent(cfg, home, fakes):
    backend = claude_backend(cfg, home)
    backend.call("reader", "p", SCHEMA, [])
    backend.call("researcher", "p", SCHEMA, RESEARCH)
    reader, researcher = fakes.calls("claude")
    assert flag(reader["argv"], "--tools") == ""
    assert "--allowedTools" not in reader["argv"]
    assert flag(researcher["argv"], "--tools") == "Read,Grep,Glob"
    allowed = flag(researcher["argv"], "--allowedTools").split(",")
    assert allowed == [
        "Read",
        "Grep",
        "Glob",
        "mcp__devtools__internet_search",
        "mcp__devtools__fetch_url",
    ]
    for call in (reader, researcher):
        assert "Agent" not in flag(call["argv"], "--tools").split(",")


def test_no_permission_prompt_can_block_a_claude_call(cfg, home, fakes):
    """The permission mode never asks, and stdin carries only the prompt and
    is then closed (the fake reads it to the end)."""
    claude_backend(cfg, home).call("author", "p", SCHEMA, ["read", "edit"])
    call = only_call(fakes, "claude")
    assert "-p" in call["argv"]
    assert flag(call["argv"], "--permission-mode") == "dontAsk"
    assert call["stdin"] == "p"


def test_researchers_can_read_target_repositories_and_knowledge_folders(
    cfg, home, fakes, tmp_path
):
    claude_backend(cfg, home).call("researcher", "p", SCHEMA, RESEARCH)
    claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    researcher, reader = fakes.calls("claude")
    argv = researcher["argv"]
    added = argv[argv.index("--add-dir") + 1 : argv.index("--add-dir") + 3]
    # A missing knowledge folder is skipped, not passed on (4.6).
    assert added == [str(tmp_path / "target-repo"), str(tmp_path / "knowledge")]
    assert "--add-dir" not in reader["argv"]


def test_a_writing_call_reaches_only_its_work_copy(cfg, home, fakes, tmp_path):
    """The author edits its work copy and never the user's checkouts or the
    knowledge folders (9.1, 9.4)."""
    work = tmp_path / "work-copy"
    backend = claude_backend(cfg, home)
    backend.call("author", "p", SCHEMA, ["read", "edit", "write"], workdir=work)
    backend.call("author", "p", SCHEMA, ["read", "edit", "write"])
    with_work, without = fakes.calls("claude")
    argv = with_work["argv"]
    assert argv[argv.index("--add-dir") + 1 :].count(str(work)) == 1
    assert str(tmp_path / "target-repo") not in argv
    assert "--add-dir" not in without["argv"]


def test_per_call_mcp_config_holds_only_the_role_servers_and_tools(cfg, home, fakes):
    backend = claude_backend(cfg, home)
    backend.call("researcher", "p", SCHEMA, RESEARCH)
    backend.call("researcher", "p", SCHEMA, [*RESEARCH, "devtools.sequential_thinking"])
    backend.call("reader", "p", SCHEMA, [])
    plain, thinking, reader = [
        json.loads(flag(c["argv"], "--mcp-config")) for c in fakes.calls("claude")
    ]
    assert plain == {
        "mcpServers": {
            "devtools": {
                "type": "stdio",
                "command": "mcp-devtools",
                "env": {"ENABLE_ADDITIONAL_TOOLS": ""},
            }
        }
    }
    devtools = thinking["mcpServers"]["devtools"]
    # Agent-launching tools never reach a call (4.4).
    assert devtools["env"]["ENABLE_ADDITIONAL_TOOLS"] == "sequential_thinking"
    assert reader == {"mcpServers": {}}


def test_researchers_reach_pr_threads_with_the_secret_left_in_the_environment(
    cfg, home, fakes, monkeypatch
):
    """The GitHub server's Authorization header is a reference that each harness
    resolves from `GITHUB_AUTH_TOKEN`; the value never appears in a command line
    (4.6)."""
    monkeypatch.setenv("GITHUB_AUTH_TOKEN", "Bearer secret-value")
    tools = [*RESEARCH, PR_THREADS]
    claude_backend(cfg, home).call("researcher", "p", SCHEMA, tools)
    codex_backend(cfg, home).call("researcher", "p", SCHEMA, tools)
    claude_call, codex_call = only_call(fakes, "claude"), only_call(fakes, "codex")

    github = json.loads(flag(claude_call["argv"], "--mcp-config"))["mcpServers"]
    assert github["github"] == {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": "${GITHUB_AUTH_TOKEN}"},
    }
    assert "mcp__github__pull_request_read" in flag(
        claude_call["argv"], "--allowedTools"
    ).split(",")
    assert _mcp_servers(codex_call["argv"])["github"] == {
        "url": "https://api.githubcopilot.com/mcp/",
        "enabled_tools": ["pull_request_read"],
        "env_http_headers": {"Authorization": "GITHUB_AUTH_TOKEN"},
    }
    for call in (claude_call, codex_call):
        assert "secret-value" not in json.dumps(call["argv"])


def test_an_environment_secret_codex_cannot_forward_is_a_config_error(
    tmp_path, make_cfg, home, fakes
):
    servers = tmp_path / "renamed.json"
    servers.write_text(
        json.dumps(
            {
                "search": {
                    "transport": {"type": "stdio", "command": "search-mcp"},
                    "secrets": {"KEY": {"env": "SEARCH_API_KEY"}},
                }
            }
        )
    )
    cfg = make_cfg(mcp={"servers_file": str(servers)})
    claude_backend(cfg, home).call("researcher", "p", SCHEMA, ["search.query"])
    server = json.loads(flag(only_call(fakes, "claude")["argv"], "--mcp-config"))
    assert server["mcpServers"]["search"]["env"] == {"SEARCH_API_KEY": "${SEARCH_KEY}"}
    with pytest.raises(DreamingError) as excinfo:
        codex_backend(cfg, home).call("researcher", "p", SCHEMA, ["search.query"])
    assert excinfo.value.code == "invalid_config"
    assert "SEARCH_API_KEY" in excinfo.value.message


def test_a_tool_on_an_undefined_server_is_a_config_error(cfg, home, fakes):
    with pytest.raises(DreamingError) as excinfo:
        claude_backend(cfg, home).call("researcher", "p", SCHEMA, ["azure.search"])
    assert excinfo.value.exit_code == USAGE_ERROR
    assert "azure" in excinfo.value.message


@pytest.mark.parametrize("tool", ["Agent", "Task", "bash", "devtools.codex-agent"])
def test_tool_lists_cannot_name_an_agent_or_unknown_tool(make_cfg, tool):
    with pytest.raises(DreamingError) as excinfo:
        make_cfg(tools={"researcher": [tool]})
    assert excinfo.value.code == "invalid_config"


def test_claude_on_lmstudio_gets_the_same_filtered_tools(cfg, home, fakes):
    claude_backend(cfg, home).call("researcher", "p", SCHEMA, RESEARCH)
    claude_backend(cfg, home, "claude-local").call("researcher", "p", SCHEMA, RESEARCH)
    cloud, local = fakes.calls("claude")
    assert local["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:1234"
    assert "ANTHROPIC_BASE_URL" not in cloud["env"]
    assert flag(local["argv"], "--model") == "qwen3-coder"
    for name in ("--mcp-config", "--tools", "--allowedTools"):
        assert flag(local["argv"], name) == flag(cloud["argv"], name)


def test_claude_errors_carry_the_raw_text(cfg, home, fakes):
    limit = "You've hit your limit · resets 3am (Europe/Dublin)"
    fakes.scenario(result_error=limit)
    with pytest.raises(BackendError) as excinfo:
        claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert limit in excinfo.value.message
    fakes.scenario(fail="Invalid API key")
    with pytest.raises(BackendError) as excinfo:
        claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert "Invalid API key" in excinfo.value.message


@pytest.mark.parametrize(
    ("answer", "expected"),
    [({"value": 1}, {"value": 1}), ("not json", "not json")],
)
def test_claude_without_structured_output_returns_the_reply(
    cfg, home, fakes, answer, expected
):
    """The reply is parsed when it is JSON and returned as text otherwise; the
    dispatcher validates it either way."""
    fakes.scenario(structured=False, answer=answer)
    result = claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert result.output == expected


def test_claude_missing_usage_is_left_for_the_estimate(cfg, home, fakes):
    fakes.scenario(no_usage=True, answer={"value": 1})
    result = claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert (result.tokens_in, result.tokens_out) == (None, None)


def test_a_call_past_its_timeout_is_killed(make_cfg, home, fakes):
    cfg = make_cfg(thresholds={"call_timeout_seconds": 1})
    fakes.scenario(sleep=30)
    started = time.monotonic()
    with pytest.raises(BackendError) as excinfo:
        claude_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert time.monotonic() - started < 10
    assert "1s" in excinfo.value.message


def test_the_calls_row_has_the_harness_session_id(cfg, db, home, fakes):
    """Recorded before the call, so a failed or interrupted call still names its
    session (1.5)."""
    fakes.scenario(answer={"value": 1})
    run_id = add_run(db, cfg=cfg)
    runner = run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader")},
        backends={"claude": claude_backend(cfg, home)},
        planner=reads(1, review=False),
        clock=FakeClock(),
    )
    assert runner.execute() == "complete"
    fakes.scenario(fail="boom")
    with pytest.raises(BackendError):
        run.Context(runner, None).attempt(
            "k", "reader", runner.backends["claude"], "p", SCHEMA
        )
    rows = db.execute("SELECT * FROM calls ORDER BY id").fetchall()
    ids = [flag(c["argv"], "--session-id") for c in fakes.calls("claude")]
    assert [row["harness_session_id"] for row in rows] == ids
    assert rows[0]["session_ref"] == ids[0]
    assert rows[1]["outcome"] == "error"


# Codex


def test_codex_call_saves_its_events_and_returns_the_final_message(cfg, home, fakes):
    fakes.scenario(answer={"value": 4})
    result = codex_backend(cfg, home).call("reader", "read this", SCHEMA, [])
    assert result.output == {"value": 4}
    assert (result.tokens_in, result.tokens_out) == (200, 40)
    thread = "0199aaaa-0000-7000-8000-00000000c0de"
    assert result.session_ref == thread
    # The event stream is kept where apsis reads Codex exec streams (5.8).
    stream = home / "runs" / RUN / "codex" / f"{thread}.jsonl"
    assert json.loads(stream.read_text().splitlines()[0])["thread_id"] == thread
    call = only_call(fakes, "codex")
    argv = call["argv"]
    assert argv[:2] == ["exec", "--json"]
    assert call["stdin"] == "read this"
    assert json.loads(open(flag(argv, "--output-schema")).read()) == SCHEMA
    assert "skills.include_instructions=false" in argv
    assert 'approval_policy="never"' in argv
    assert flag(argv, "--sandbox") == "read-only"
    assert call["cwd"].startswith(str(home / "runs" / RUN))


def test_codex_is_killed_on_an_agent_launch_event(cfg, home, fakes):
    fakes.scenario(launch_agent=True)
    started = time.monotonic()
    with pytest.raises(BackendError) as excinfo:
        codex_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert time.monotonic() - started < 10
    assert "agent" in excinfo.value.message
    stream = (
        home / "runs" / RUN / "codex" / "0199aaaa-0000-7000-8000-00000000c0de.jsonl"
    )
    assert "collab_tool_call" in stream.read_text()


def _mcp_servers(argv):
    (value,) = [a.split("=", 1)[1] for a in argv if a.startswith("mcp_servers=")]
    return tomllib.loads(f"servers = {value}")["servers"]


def test_codex_mcp_servers_are_the_filtered_role_servers(cfg, home, fakes):
    backend = codex_backend(cfg, home)
    backend.call("researcher", "p", SCHEMA, RESEARCH)
    backend.call("reader", "p", SCHEMA, [])
    researcher, reader = [c["argv"] for c in fakes.calls("codex")]
    assert _mcp_servers(researcher) == {
        "devtools": {
            "command": "mcp-devtools",
            "env": {"ENABLE_ADDITIONAL_TOOLS": ""},
            "enabled_tools": ["internet_search", "fetch_url"],
        }
    }
    assert _mcp_servers(reader) == {}


def test_codex_writes_only_in_the_work_copy(cfg, home, fakes, tmp_path):
    work = tmp_path / "work-copy"
    backend = codex_backend(cfg, home)
    backend.call("author", "p", SCHEMA, ["read", "edit", "write"], workdir=work)
    backend.call("author", "p", SCHEMA, ["read", "edit", "write"])
    backend.call("researcher", "p", SCHEMA, RESEARCH, workdir=work)
    with_work, without, reading = [c["argv"] for c in fakes.calls("codex")]
    assert flag(with_work, "--sandbox") == "workspace-write"
    assert flag(with_work, "--add-dir") == str(work)
    # Without a work copy, or without a writing tool, nothing is writable (9.4).
    for argv in (without, reading):
        assert flag(argv, "--sandbox") == "read-only"
        assert "--add-dir" not in argv


def test_codex_on_lmstudio_gets_the_same_filtered_tools(cfg, home, fakes):
    codex_backend(cfg, home).call("researcher", "p", SCHEMA, RESEARCH)
    codex_backend(cfg, home, "codex-local").call("researcher", "p", SCHEMA, RESEARCH)
    cloud, local = [c["argv"] for c in fakes.calls("codex")]
    assert "--oss" in local and flag(local, "--local-provider") == "lmstudio"
    assert _mcp_servers(local) == _mcp_servers(cloud)


def test_codex_uses_the_isolated_home_when_the_probe_chose_it(cfg, home, fakes):
    codex_backend(cfg, home).call("reader", "p", SCHEMA, [])
    codex_backend(cfg, home, isolation="isolated-home").call("reader", "p", SCHEMA, [])
    user, isolated = fakes.calls("codex")
    assert user["codex_home"] is None
    assert isolated["codex_home"] == str(home / "codex-home")


def test_codex_errors_carry_the_raw_text(cfg, home, fakes):
    limit = "You've hit your usage limit. Upgrade to Pro or try again at 4:18 PM."
    fakes.scenario(exec_fail=True, exec_error=limit)
    with pytest.raises(BackendError) as excinfo:
        codex_backend(cfg, home).call("reader", "p", SCHEMA, [])
    assert limit in excinfo.value.message


# Adapters from probe results


@pytest.mark.parametrize(
    ("name", "isolation", "kind"),
    [
        ("claude", "setting-sources", claude.ClaudeBackend),
        ("codex", "isolated-home", codex.CodexBackend),
        ("lmstudio", "direct", lmstudio.LMStudioBackend),
    ],
)
def test_a_usable_probe_result_gives_an_adapter_with_its_isolation(
    make_cfg, home, name, isolation, kind
):
    cfg = make_cfg(backends={"lmstudio": {"harness": "lmstudio", "context_tokens": 8}})
    result = probe.ProbeResult(
        name,
        cfg["backends"][name]["harness"],
        "1.0",
        status="probed",
        isolation=isolation,
        agent_enforcement="removed",
        apsis_finds_session=True,
    )
    backend = probe.adapter(home, cfg, RUN, result)
    assert isinstance(backend, kind)
    assert (backend.name, backend.run_id) == (name, RUN)
    if name == "claude":
        assert backend.isolation.name == isolation
    if name == "codex":
        assert backend.isolation == isolation


def test_an_unusable_probe_result_gives_no_adapter(cfg, home):
    leaking = probe.ProbeResult(
        "codex",
        "codex",
        "1.0",
        status="probed",
        isolation="user-home",
        leaks=["skills"],
        agent_enforcement="kill_on_agent_event",
        apsis_finds_session=True,
    )
    assert probe.adapter(home, cfg, RUN, leaking) is None

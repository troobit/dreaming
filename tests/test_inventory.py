"""The tooling inventory and context weight (8.1, 8.2, 10.4)."""

import json
from pathlib import Path

import pytest
from dreamkit import ScriptedModel, answering, dream, observation
from reportkit import page, section
from sessiondocs import StubApsis, days_ago, make_doc, make_entry

from dreaming import config, inventory, report
from dreaming.backends import estimate_tokens

RUN = "dream-test"
SYSTEM = "You are a coding agent. Follow the house rules. " * 60
SHELL = {
    "type": "function",
    "name": "shell",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
}
CLAUDE_MD = "# House rules\n\n" + "- Prefer small commits.\n" * 30
SKILL = "---\nname: commit\ndescription: Commit staged work with a message.\n---\n\n"
SKILL_BODY = "Long instructions the listing does not load.\n" * 50


def tool(name, **given):
    return {"type": "tool_use", "id": f"t-{name}", "name": name, "input": given}


def claude_doc():
    when = days_ago(2)
    entries = [
        make_entry("user", "need: look this up on the web", when, id="c-u0"),
        {
            "role": "assistant",
            "id": "c-a0",
            "timestamp": when,
            "content": [
                tool("Read", file_path="a.py"),
                tool("mcp__devtools__internet_search", query="uv"),
                tool("mcp__devtools__internet_search", query="ruff"),
                tool("Skill", skill="commit"),
            ],
        },
    ]
    return make_doc("claude", "c1", when, entries=entries)


def lmstudio_doc():
    when = days_ago(3)
    entries = [
        make_entry("system", SYSTEM, when),
        make_entry("user", "fix the failing test", when),
        {
            "role": "assistant",
            "timestamp": when,
            "content": [tool("shell", command="make test")],
        },
    ]
    doc = make_doc(
        "lmstudio",
        "lm1",
        when,
        entries=entries,
        thread_id="lm-thread-1",
        model="qwen3-coder-30b",
        provider="lmstudio",
        project=None,
    )
    doc["requests"] = [
        {
            "id": "req-1",
            "thread_id": "lm-thread-1",
            "model": "qwen3-coder-30b",
            "parameters": {"tools": [SHELL]},
        }
    ]
    return doc


def codex_exec_doc():
    when = days_ago(4)
    usage = {
        "type": "usage",
        "basis": "thread_total_at_turn_completion",
        "counters": {
            "input_tokens": 9000,
            "cached_input_tokens": 0,
            "output_tokens": 9,
        },
    }
    entries = [
        {
            "role": "assistant",
            "turn": 1,
            "timestamp": when,
            "content": [tool("command_execution", command="ls"), usage],
        },
        {
            "role": "assistant",
            "turn": 2,
            "timestamp": when,
            "content": [{**usage, "counters": {"input_tokens": 20000}}],
        },
    ]
    return make_doc("codex-exec", "x1", when, entries=entries, thread_id="x-thread")


def write(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def harness_files(tmp_path):
    """Each harness's own configuration in the isolated HOME, and the shared
    MCP definitions."""
    write(
        "~/.claude.json",
        json.dumps(
            {
                "mcpServers": {"devtools": {}, "svelte": {}},
                "projects": {"/work/repo": {"mcpServers": {"github": {}}}},
            }
        ),
    )
    write("~/.codex/config.toml", '[mcp_servers.devtools]\ncommand = "mcp-devtools"\n')
    write("~/.claude/CLAUDE.md", CLAUDE_MD)
    write("~/.claude/skills/commit/SKILL.md", SKILL + SKILL_BODY)
    shared = {name: {} for name in ("devtools", "svelte", "github", "transit")}
    return write(tmp_path / "servers.json", json.dumps(shared))


@pytest.fixture
def cfg(harness_files):
    return config.build_config(
        {
            "backends": {
                "fake": {"harness": "claude", "context_tokens": 100_000},
                "local": {
                    "harness": "codex",
                    "provider": "lmstudio",
                    "model": "qwen3-coder-30b",
                    "context_tokens": 2000,
                },
            },
            "roles": {role: {"backends": ["fake"]} for role in config.ROLES},
            "mcp": {"servers_file": str(harness_files)},
        }
    )


def needs(data):
    """A reader noting a need a tool could have met in each `need:` prompt."""
    found = []
    for entry in data["entries"]:
        if entry["text"].startswith("need:"):
            found.append(
                observation(entry, entry["text"], "web lookup", lens="mcp-tool-use")
            )
    return {"observations": found}


@pytest.fixture
def recorded(db, home, cfg):
    apsis = StubApsis(claude_doc(), lmstudio_doc(), codex_exec_doc())
    assert dream(db, home, cfg, apsis, ScriptedModel(answering(needs))) == "complete"
    inventory.record(db, cfg, RUN)
    return inventory.load(db, RUN)


# What a session used (8.1)


def test_the_read_step_records_each_sessions_tools_skills_and_first_request(
    db, home, cfg
):
    apsis = StubApsis(claude_doc(), lmstudio_doc(), codex_exec_doc())
    dream(db, home, cfg, apsis, ScriptedModel(answering(needs)))
    used = {
        row["session_id"]: json.loads(row["inventory"])
        for row in db.execute("SELECT * FROM sessions_seen")
    }
    assert used["c1"] == {
        "tools": {
            "Read": 1,
            "Skill": 1,
            "mcp__devtools__internet_search": 2,
        },
        "skills": {"commit": 1},
        "first_request": {"measured": None, "instructions": None, "tool_schemas": {}},
    }
    assert used["lm1"]["first_request"] == {
        "measured": None,
        "instructions": estimate_tokens(SYSTEM),
        "tool_schemas": {"shell": estimate_tokens(json.dumps(SHELL))},
    }
    assert used["x1"]["tools"] == {"command_execution": 1}
    assert used["x1"]["first_request"]["measured"] == 9000


# MCP servers and tools per harness (8.1)


def test_configured_servers_are_compared_with_their_use(recorded):
    claude = recorded["harnesses"]["claude"]
    assert claude["configured"] == ["devtools", "github", "svelte"]
    assert claude["available"] == ["transit"]
    assert claude["servers"] == {"devtools": {"internet_search": 2}}
    assert claude["tools"] == {"Read": 1, "Skill": 1}
    assert claude["skills"] == {"commit": 1}
    assert claude["never_used"] == ["github", "svelte"]
    assert claude["sessions"] == 1
    codex = recorded["harnesses"]["codex"]
    assert codex["configured"] == ["devtools"]
    assert codex["never_used"] == ["devtools"]
    assert codex["tools"] == {"command_execution": 1}
    assert recorded["harnesses"]["lmstudio"]["tools"] == {"shell": 1}


def test_sessions_showing_a_need_a_tool_could_meet_are_listed(recorded):
    (need,) = recorded["needs"]
    assert need["session"] == "claude:c1"
    assert need["quote"] == "need: look this up on the web"
    assert need["key"] == "web lookup"


def test_an_unreadable_configuration_is_reported_not_fatal(db, home, cfg):
    write("~/.codex/config.toml", "this is [not toml")
    dream(db, home, cfg, StubApsis(claude_doc()), ScriptedModel(answering(needs)))
    inventory.record(db, cfg, RUN)
    found = inventory.load(db, RUN)
    assert found["harnesses"]["codex"]["configured"] == []
    (problem,) = found["problems"]
    assert "config.toml" in problem


# Context before the first prompt (8.2)


def rows_by_harness(found):
    return {row["harness"]: row for row in found["context"]}


def test_context_weight_is_labelled_measured_estimated_or_unavailable(recorded):
    rows = rows_by_harness(recorded)
    claude = rows["claude"]
    assert claude["instructions"] == {
        "label": "estimated",
        "tokens": estimate_tokens(CLAUDE_MD),
        "from": "instruction files",
    }
    assert claude["skills"] == {
        "label": "estimated",
        "tokens": estimate_tokens(SKILL.split("\n---")[0] + "\n---"),
        "from": "skill listings",
    }
    assert claude["tool_schemas"]["label"] == "unavailable"
    assert claude["measured"]["label"] == "unavailable"
    assert claude["total"]["label"] == "estimated"
    assert claude["window"] is None and claude["flagged"] is False

    lmstudio = rows["lmstudio"]
    assert lmstudio["model"] == "qwen3-coder-30b"
    assert lmstudio["instructions"]["from"] == "the session's system text"
    assert lmstudio["tool_schemas"]["tokens"] == estimate_tokens(json.dumps(SHELL))
    assert lmstudio["skills"]["label"] == "unavailable"

    codex = rows["codex"]
    assert codex["measured"] == {
        "label": "measured",
        "tokens": 9000,
        "from": "the first turn's input tokens",
    }
    assert codex["total"] == {"label": "measured", "tokens": 9000}


def test_the_largest_contributors_are_named(recorded):
    rows = rows_by_harness(recorded)
    names = [c["name"] for c in rows["lmstudio"]["contributors"]]
    assert names == ["system text", "tool shell"]
    claude = [c["name"] for c in rows["claude"]["contributors"]]
    assert claude == ["~/.claude/CLAUDE.md", "skill commit"]


def test_consumption_above_the_share_of_a_known_window_is_flagged(recorded):
    lmstudio = rows_by_harness(recorded)["lmstudio"]
    # The configured backend with this model has a 2000-token window; the
    # system text alone is over a quarter of it.
    assert lmstudio["window"] == 2000
    assert lmstudio["flagged"] is True


# The report (10.4)


def test_the_report_shows_the_inventory_recorded_at_the_end_of_the_run(
    db, cfg, recorded
):
    shown = section(page(db, RUN, cfg), "inventory")
    assert "devtools" in shown and "internet_search" in shown
    assert "Never used" in shown and "github" in shown
    assert "need: look this up on the web" in shown
    assert "estimated" in shown and "measured" in shown and "unavailable" in shown
    assert "over 25% of the window" in shown


def test_writing_the_report_records_the_inventory(db, home, cfg):
    dream(db, home, cfg, StubApsis(claude_doc()), ScriptedModel(answering(needs)))
    report.write(db, RUN, home, cfg)
    assert inventory.load(db, RUN)["harnesses"]["claude"]["sessions"] == 1

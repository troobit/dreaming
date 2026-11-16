import json

import pytest

from dreaming.config import load_config
from dreaming.sessions import ContentBlock, Entry, SessionDocument, SessionMetadata


def block(type_, **data):
    return ContentBlock(type_, {"type": type_, **data})


def document(source, *blocks, requests=(), sid="s1"):
    return SessionDocument(
        SessionMetadata(source=source, id=sid),
        (Entry(role="assistant", content=tuple(blocks)),),
        (),
        tuple(requests),
    )


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "userhome"
    (root / ".claude" / "skills" / "big").mkdir(parents=True)
    (root / ".claude" / "skills" / "tiny").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("x" * 4000)
    (root / ".claude" / "skills" / "big" / "SKILL.md").write_text(
        "---\nname: big\ndescription: " + "d" * 400 + "\n---\nbody " * 1000
    )
    (root / ".claude" / "skills" / "tiny" / "SKILL.md").write_text(
        "---\nname: tiny\ndescription: small\n---\nbody"
    )
    (root / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"devtools": {"command": "x"}, "unused": {"command": "y"}},
                "projects": {"/p": {"mcpServers": {"extra": {"command": "z"}}}},
            }
        )
    )
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["mcp__devtools__think", "Bash(ls)"]}})
    )
    (root / ".codex").mkdir()
    (root / ".codex" / "config.toml").write_text(
        '[mcp_servers.docs]\ncommand = "d"\nenabled_tools = ["search"]\n'
    )
    (root / ".codex" / "AGENTS.md").write_text("y" * 400)
    return root


@pytest.fixture
def config(tmp_path):
    return load_config(
        tmp_path / "config.toml",
        overrides={
            "thresholds": {"context_warning_share": 0.05},
            "backends": {
                "local": {"harness": "lmstudio", "context_tokens": 2000, "model": "m"},
            },
        },
    )


def test_configured_servers_and_tools_per_harness(home):
    from dreaming.inventory import configured_tools

    tools = configured_tools(home)
    assert set(tools["claude"]) == {"devtools", "unused", "extra"}
    assert tools["claude"]["devtools"] == ("think",)
    assert tools["codex"] == {"docs": ("search",)}


def test_missing_configuration_is_empty(tmp_path):
    from dreaming.inventory import configured_tools, context_contributors

    assert configured_tools(tmp_path) == {}
    assert context_contributors(tmp_path) == {}


def test_usage_counts_servers_tools_and_skills(home):
    from dreaming.inventory import configured_tools, tool_usage

    docs = [
        document(
            "claude",
            block("tool_use", name="mcp__devtools__think", input={}),
            block("tool_use", name="mcp__devtools__think", input={}),
            block("tool_use", name="Skill", input={"skill": "big"}),
            block("tool_use", name="Bash", input={}),
        ),
        document(
            "codex-exec",
            block("tool_use", name="mcp_tool_call", input={"server": "docs", "tool": "search"}),
            sid="s2",
        ),
    ]
    usage = tool_usage(docs)
    assert usage.mcp[("claude", "devtools", "think")] == 2
    assert usage.mcp[("codex", "docs", "search")] == 1
    assert usage.skills[("claude", "big")] == 1
    assert usage.native[("claude", "Bash")] == 1
    assert usage.sessions["claude"] == 1
    unused = usage.never_used(configured_tools(home))
    assert ("claude", "unused") in unused
    assert ("claude", "devtools") not in unused
    assert ("codex", "docs") not in unused


def test_need_detected_when_unavailable_tool_exists_elsewhere(home):
    from dreaming.inventory import configured_tools, tool_usage

    failing = [
        document(
            "claude",
            block("tool_use", id="t1", name="mcp__docs__search", input={}),
            block(
                "tool_result",
                tool_use_id="t1",
                content="Error: No such tool available",
                is_error=True,
            ),
        ),
        document(
            "claude",
            block("tool_use", id="t1", name="mcp__nowhere__x", input={}),
            block("tool_result", tool_use_id="t1", content="No such tool available", is_error=True),
            sid="s2",
        ),
        document(
            "claude",
            block("tool_use", id="t1", name="Bash", input={}),
            block("tool_result", tool_use_id="t1", content="exit 1", is_error=True),
            sid="s3",
        ),
    ]
    needs = tool_usage(failing).needs(configured_tools(home))
    assert [(n.session, n.tool) for n in needs] == [("s1", "mcp__docs__search")]
    assert needs[0].available_in == "codex"


def test_context_contributors_are_estimated_and_largest_first(home):
    from dreaming.inventory import context_contributors

    items = context_contributors(home)["claude"]
    assert items[0].name == "instructions: CLAUDE.md"
    big = next(i for i in items if i.name == "skill listing: big")
    assert all(i.basis == "estimated" for i in items)
    assert {i.name for i in items} >= {"instructions: CLAUDE.md", "skill listing: tiny"}
    assert items == sorted(items, key=lambda i: -i.tokens)
    # Listing weight counts name and description, not the skill body.
    assert big.tokens < 200


def test_build_labels_measured_estimated_and_unavailable(home, config):
    from dreaming.inventory import build_inventory

    usage_doc = document(
        "claude",
        block(
            "usage",
            basis="first_request",
            counters={"input_tokens": 100, "cache_read_input_tokens": 900},
        ),
    )
    lm = document(
        "lmstudio",
        requests=[{"id": "r", "parameters": {"tools": [{"name": "t", "description": "d" * 400}]}}],
    )
    inventory = build_inventory(config, home, [usage_doc, lm])
    rows = {r.backend: r for r in inventory.rows}
    claude = rows["claude"]
    assert claude.measured == 1000
    assert claude.tools_basis == "unavailable"
    assert claude.instructions_basis == "estimated"
    assert claude.window == 200_000 and not claude.flagged
    codex = rows["codex"]
    assert codex.measured is None and codex.measured_basis == "unavailable"
    local = rows["local"]
    assert local.tools_basis == "estimated" and local.tools > 100
    assert local.instructions_basis == "unavailable"
    assert local.flagged and local.flag_basis == "estimated"


def test_measured_above_share_is_flagged(home, config):
    from dreaming.inventory import build_inventory

    doc = document(
        "claude",
        block("usage", basis="first_request", counters={"input_tokens": 50_000}),
    )
    row = {r.backend: r for r in build_inventory(config, home, [doc]).rows}["claude"]
    assert row.flagged and row.flag_basis == "measured"


def test_thread_totals_are_not_treated_as_first_prompt_context(home, config):
    from dreaming.inventory import build_inventory

    doc = document(
        "codex-exec",
        block("usage", basis="thread_total_at_turn_completion", counters={"input_tokens": 90_000}),
    )
    row = {r.backend: r for r in build_inventory(config, home, [doc]).rows}["codex"]
    assert row.measured is None


def test_render_escapes_and_names_contributors(home, config):
    from dreaming.inventory import build_inventory, render_inventory

    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"<script>alert(1)</script>": {}}})
    )
    page = render_inventory(build_inventory(config, home, []))
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "skill listing: big" in page or "instructions: CLAUDE.md" in page
    assert "never used" in page.lower()
    assert "unavailable" in page


def test_report_section_reads_sessions_of_the_run(tmp_path, home, config):
    from dreaming.inventory import inventory_section
    from dreaming.report import render_report
    from dreaming.store import Store

    class Client:
        def open(self, session_id, source):
            assert (session_id, source) == ("s1", "claude")
            return document("claude", block("tool_use", name="mcp__devtools__think", input={}))

    with Store(tmp_path / "dh") as store:
        c = store.connection
        c.execute(
            "INSERT INTO runs(id,status,revision,started_at,statistics,budget) "
            "VALUES ('r1','complete','rev','2026-11-15T00:00:00Z','{}','{}')"
        )
        for sid in ("s1", "gone"):
            c.execute(
                "INSERT INTO sessions_seen(source,session_id,state,forgotten) "
                "VALUES ('claude',?,'read',?)",
                (sid, int(sid == "gone")),
            )
            c.execute(
                "INSERT INTO steps(run_id,key,phase,state) VALUES ('r1',?,'Read','done')", (sid,)
            )
            c.execute(
                "INSERT INTO calls(id,run_id,task_key,attempt,role,backend) "
                "VALUES (?,'r1',?,1,'reader','claude')",
                ("call-" + sid, sid),
            )
            c.execute(
                "INSERT INTO session_inputs(call_id,source,session_id) VALUES (?,'claude',?)",
                ("call-" + sid, sid),
            )
        section = inventory_section(store, "r1", config, Client(), home)
        page = render_report(store, "r1", sections={"inventory": section})
    assert "devtools" in page and "think" in page

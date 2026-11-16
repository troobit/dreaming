"""Tooling inventory and context weight (requirements 8.1, 8.2).

Reads harness configuration files and shared session documents. Nothing here makes a
model call or changes persistent state. Values are labelled ``measured`` (reported by a
session document), ``estimated`` (bytes divided by four) or ``unavailable``.
"""

import html
import json
import math
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from dreaming.config import Config
from dreaming.redact import redact
from dreaming.sessions import SessionDocument, SessionError

HARNESS_OF_SOURCE = {"claude": "claude", "codex": "codex", "codex-exec": "codex"}
_UNAVAILABLE_TOOL = ("no such tool", "unknown tool", "tool not found", "not available")
_FIRST_REQUEST_COUNTERS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _harness(source: str | None) -> str:
    return HARNESS_OF_SOURCE.get(source or "", source or "unknown")


def _tokens(size: int) -> int:
    return math.ceil(size / 4)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def configured_tools(home: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Map harness to MCP server to explicitly configured tool names (possibly none)."""
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    claude = _read_json(home / ".claude.json")
    servers = dict(claude.get("mcpServers") or {})
    for project in (claude.get("projects") or {}).values():
        if isinstance(project, dict):
            servers.update(project.get("mcpServers") or {})
    allowed = (_read_json(home / ".claude" / "settings.json").get("permissions") or {}).get(
        "allow"
    ) or []
    claude_tools: dict[str, list[str]] = {name: [] for name in servers}
    for rule in allowed:
        parts = rule.split("__") if isinstance(rule, str) else []
        if len(parts) == 3 and parts[0] == "mcp" and parts[1] in claude_tools and parts[2] != "*":
            claude_tools[parts[1]].append(parts[2])
    if claude_tools:
        result["claude"] = {name: tuple(tools) for name, tools in claude_tools.items()}
    codex = (_read_toml(home / ".codex" / "config.toml").get("mcp_servers")) or {}
    if codex:
        result["codex"] = {
            name: tuple(t for t in (cfg or {}).get("enabled_tools", []) if isinstance(t, str))
            for name, cfg in codex.items()
        }
    return result


@dataclass(frozen=True)
class Contributor:
    name: str
    tokens: int
    basis: str = "estimated"


def _skill_listing(path: Path) -> int:
    """Size of a skill's name and description, which is all a listing carries."""
    size = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith(("name:", "description:")):
                size += len(line.encode())
    return size


def context_contributors(home: Path) -> dict[str, list[Contributor]]:
    """Per harness, estimated instruction and skill-listing weight, largest first."""
    layout = {
        "claude": (home / ".claude", "CLAUDE.md"),
        "codex": (home / ".codex", "AGENTS.md"),
    }
    result: dict[str, list[Contributor]] = {}
    for harness, (root, instruction) in layout.items():
        items = []
        files = [root / instruction, *sorted((root / "rules").glob("**/*.md"))]
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append(Contributor(f"instructions: {path.relative_to(root)}", _tokens(size)))
        for skill in sorted((root / "skills").glob("*/SKILL.md")):
            if size := _skill_listing(skill):
                items.append(Contributor(f"skill listing: {skill.parent.name}", _tokens(size)))
        if items:
            result[harness] = sorted(items, key=lambda i: -i.tokens)
    return result


@dataclass(frozen=True)
class Need:
    harness: str
    session: str
    tool: str
    available_in: str


def _server_tool(name: str, data: dict) -> tuple[str, str] | None:
    parts = name.split("__")
    if len(parts) >= 3 and parts[0] == "mcp":
        return parts[1], "__".join(parts[2:])
    if name == "mcp_tool_call":
        inp = data.get("input")
        if isinstance(inp, dict) and isinstance(inp.get("server"), str):
            return inp["server"], str(inp.get("tool", ""))
    return None


@dataclass
class Usage:
    mcp: Counter = field(default_factory=Counter)  # (harness, server, tool)
    skills: Counter = field(default_factory=Counter)  # (harness, skill)
    native: Counter = field(default_factory=Counter)  # (harness, tool)
    sessions: Counter = field(default_factory=Counter)  # harness
    failed: list = field(default_factory=list)  # (harness, session, server, tool, name)
    first_request: dict = field(default_factory=dict)  # harness -> smallest measured tokens
    request_tools: dict = field(default_factory=dict)  # harness -> largest tools schema bytes

    def never_used(self, configured) -> list[tuple[str, str]]:
        used = {(h, s) for h, s, _ in self.mcp}
        return [(h, s) for h, servers in configured.items() for s in servers if (h, s) not in used]

    def needs(self, configured) -> list[Need]:
        """Sessions that attempted an unavailable tool configured for another harness."""
        found = []
        for harness, session, server, tool, name in self.failed:
            owners = [
                h
                for h, servers in configured.items()
                if h != harness
                and server in servers
                and (not servers[server] or tool in servers[server])
            ]
            own = configured.get(harness, {})
            if owners and server not in own:
                found.append(Need(harness, session, name, owners[0]))
        return found


def tool_usage(documents) -> Usage:
    usage = Usage()
    for doc in documents:
        harness = _harness(doc.metadata.source)
        usage.sessions[harness] += 1
        session = doc.metadata.id or ""
        calls: dict[str, tuple] = {}
        measured = None
        for entry in doc.entries:
            for b in entry.content:
                data = b.data
                if b.type == "tool_use":
                    name = data.get("name") or ""
                    pair = _server_tool(name, data)
                    if pair:
                        usage.mcp[(harness, *pair)] += 1
                    elif name == "Skill" and isinstance(data.get("input"), dict):
                        usage.skills[(harness, str(data["input"].get("skill", "")))] += 1
                    else:
                        usage.native[(harness, name)] += 1
                    if data.get("id"):
                        calls[data["id"]] = (pair, name)
                elif b.type == "tool_result" and data.get("is_error"):
                    text = str(data.get("content", "")).lower()
                    pair, name = calls.get(data.get("tool_use_id"), (None, ""))
                    if pair and any(m in text for m in _UNAVAILABLE_TOOL):
                        usage.failed.append((harness, session, *pair, name))
                elif b.type == "usage" and measured is None:
                    if "total" not in str(data.get("basis", "")):
                        counters = data.get("counters") or {}
                        values = [counters.get(k) for k in _FIRST_REQUEST_COUNTERS]
                        values = [v for v in values if type(v) is int]
                        measured = sum(values) if values else None
        if measured is not None:
            usage.first_request[harness] = min(measured, usage.first_request.get(harness, measured))
        for request in doc.requests:
            tools = (request.get("parameters") or {}).get("tools")
            if tools:
                size = len(json.dumps(tools, ensure_ascii=False).encode())
                usage.request_tools[harness] = max(size, usage.request_tools.get(harness, 0))
    return usage


@dataclass(frozen=True)
class ContextRow:
    backend: str
    harness: str
    window: int
    instructions: int | None
    instructions_basis: str
    skills: int | None
    tools: int | None
    tools_basis: str
    measured: int | None
    measured_basis: str
    flagged: bool
    flag_basis: str | None
    contributors: tuple[Contributor, ...]


@dataclass(frozen=True)
class Inventory:
    configured: dict
    usage: Usage
    needs: tuple[Need, ...]
    never_used: tuple[tuple[str, str], ...]
    rows: tuple[ContextRow, ...]
    share: float


def build_inventory(config: Config, home: Path, documents) -> Inventory:
    configured = configured_tools(home)
    contributors = context_contributors(home)
    usage = tool_usage(documents)
    rows = []
    for name, backend in config.backends.items():
        harness = backend.harness
        items = contributors.get(harness, [])
        instructions = sum(i.tokens for i in items if i.name.startswith("instructions"))
        skills = sum(i.tokens for i in items if i.name.startswith("skill listing"))
        tools_size = usage.request_tools.get(harness)
        measured = usage.first_request.get(harness)
        known = bool(items)
        estimate = (instructions + skills if known else 0) + (
            _tokens(tools_size) if tools_size else 0
        )
        limit = config.thresholds.context_warning_share * backend.context_tokens
        basis = (
            "measured"
            if measured is not None and measured > limit
            else "estimated"
            if estimate > limit
            else None
        )
        rows.append(
            ContextRow(
                backend=name,
                harness=harness,
                window=backend.context_tokens,
                instructions=instructions if known else None,
                instructions_basis="estimated" if known else "unavailable",
                skills=skills if known else None,
                tools=_tokens(tools_size) if tools_size else None,
                tools_basis="estimated" if tools_size else "unavailable",
                measured=measured,
                measured_basis="measured" if measured is not None else "unavailable",
                flagged=basis is not None,
                flag_basis=basis,
                contributors=tuple(items[:5]),
            )
        )
    return Inventory(
        configured,
        usage,
        tuple(usage.needs(configured)),
        tuple(usage.never_used(configured)),
        tuple(rows),
        config.thresholds.context_warning_share,
    )


def _e(value) -> str:
    return html.escape(redact("" if value is None else str(value)), quote=True)


def _table(headings, rows) -> str:
    if not rows:
        return '<p class="muted">None recorded.</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headings)
    body = "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in rows)
    return f'<table class="findings"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _label(value, basis) -> str:
    return "unavailable" if value is None else f"{value} tokens ({basis})"


def render_inventory(inv: Inventory) -> str:
    """Trusted HTML for the report's ``inventory`` section; every value is escaped."""
    unused = set(inv.never_used)
    tools = []
    for harness, servers in sorted(inv.configured.items()):
        for server, names in sorted(servers.items()):
            counts = {t: n for (h, s, t), n in inv.usage.mcp.items() if (h, s) == (harness, server)}
            used = sum(counts.values())
            detail = ", ".join(f"{t} x{n}" for t, n in sorted(counts.items())) or "none"
            tools.append(
                (
                    harness,
                    server,
                    ", ".join(names) or "not listed",
                    used,
                    "never used" if (harness, server) in unused else detail,
                )
            )
    skills = [(h, s, n) for (h, s), n in sorted(inv.usage.skills.items())]
    needs = [(n.harness, n.session, n.tool, f"configured for {n.available_in}") for n in inv.needs]
    context = []
    for r in inv.rows:
        flag = f"above {inv.share:.0%} of window ({r.flag_basis})" if r.flagged else "within share"
        context.append(
            (
                r.backend,
                r.harness,
                r.window,
                _label(r.measured, "measured"),
                _label(r.instructions, r.instructions_basis),
                _label(r.skills, "estimated"),
                _label(r.tools, r.tools_basis),
                flag,
                "; ".join(f"{c.name} ({c.tokens})" for c in r.contributors) or "none identified",
            )
        )
    return (
        "<h3>MCP servers and tools</h3>"
        + _table(("Harness", "Server", "Configured tools", "Calls", "Usage"), tools)
        + "<h3>Skills used</h3>"
        + _table(("Harness", "Skill", "Uses"), skills)
        + "<h3>Possible unmet needs</h3>"
        + _table(("Harness", "Session", "Attempted tool", "Available"), needs)
        + "<h3>Context before the first prompt</h3>"
        + _table(
            (
                "Backend",
                "Harness",
                "Window",
                "Measured total",
                "Instructions",
                "Skill listings",
                "Tool schemas",
                "Flag",
                "Largest contributors",
            ),
            context,
        )
    )


def inventory_section(store, run_id: str, config: Config, client, home: Path | None = None) -> str:
    """Render the inventory from the run's read, non-forgotten sessions."""
    rows = store.connection.execute(
        "SELECT DISTINCT s.source,s.session_id FROM session_inputs i "
        "JOIN calls c ON c.id=i.call_id "
        "JOIN sessions_seen s ON s.source=i.source AND s.session_id=i.session_id "
        "WHERE c.run_id=? AND c.role='reader' AND s.forgotten=0 ORDER BY 1,2",
        (run_id,),
    ).fetchall()
    documents: list[SessionDocument] = []
    for source, session_id in rows:
        try:
            documents.append(client.open(session_id, source))
        except SessionError:
            continue
    return render_inventory(build_inventory(config, home or Path.home(), documents))

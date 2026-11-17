"""The tooling inventory and context weight (8.1, 8.2, 10.4).

While a Read step has a session's document open, `session_use` counts the tools
and skills the part read used, and what the document shows of the context
before the first user prompt: input tokens measured on the first request when
the document carries a usage record, and the size of the system text and tool
schemas that request sent (LM Studio's server log records both). The result is
stored with the session.

As a run ends, `record` builds its inventory from the sessions it read and each
harness's own configuration files, and stores it with the run, so the report
shows what was true when the dream ended:

- per harness: the MCP servers its files configure, the servers the shared
  definitions offer that it does not configure, each tool's use in the sessions
  read (MCP tools by server), the configured servers no session used, and the
  needs the `mcp-tool-use` lens observed;
- per harness and model: the context before the first prompt by instructions,
  skill listings and tool schemas, each labelled measured, estimated or
  unavailable, the largest contributors, and a flag when the total exceeds
  `context_weight_share` of a known window, the context of a configured
  backend with that model.

Estimates are `utf8 bytes / 4`, as for every other estimate here. Files that
cannot be read are listed as problems and never stop a dream.
"""

import glob
import json
import sqlite3
import tomllib
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from dreaming import knowledge, sessions
from dreaming.backends import estimate_tokens, mcp
from dreaming.errors import DreamingError

# Claude Code's skill tool names the skill it loads in its input.
SKILL_TOOL = "Skill"
NEEDS_LENS = "mcp-tool-use"
CONTRIBUTORS = 5


@dataclass(frozen=True)
class HarnessFiles:
    mcp: tuple[str, ...]  # configuration files naming MCP servers
    instructions: tuple[str, ...]  # instruction files loaded at start (globs)
    skills: tuple[str, ...]  # skill files whose listing is loaded (globs)


HARNESS_FILES = {
    "claude": HarnessFiles(
        ("~/.claude.json",), ("~/.claude/CLAUDE.md",), ("~/.claude/skills/*/SKILL.md",)
    ),
    "codex": HarnessFiles(
        ("~/.codex/config.toml",),
        ("~/.codex/AGENTS.md",),
        ("~/.codex/skills/*/SKILL.md",),
    ),
    "kiro": HarnessFiles(
        ("~/.kiro/settings/mcp.json",), ("~/.kiro/steering/*.md",), ()
    ),
    "lmstudio": HarnessFiles(("~/.lmstudio/mcp.json",), (), ()),
}


# What one session used


def _blocks(document: sessions.SessionDocument) -> Iterator[dict]:
    for entry in document.entries:
        yield from entry.content


def first_request(document: sessions.SessionDocument) -> dict:
    """The context before the first user prompt as the document shows it: the
    first usage record's input tokens, and the estimated size of the system
    text before the first user entry and of the first request's tool schemas."""
    measured = next(
        (
            block.get("counters", {}).get("input_tokens")
            for block in _blocks(document)
            if block.get("type") == "usage"
        ),
        None,
    )
    system = []
    for entry in document.entries:
        if entry.role == "user":
            break
        if entry.role == "system" and not entry.unattributed:
            system.append(sessions.entry_text(entry))
    request = document.requests[0] if document.requests else {}
    tools = (request.get("parameters") or {}).get("tools") or []
    return {
        "measured": measured if isinstance(measured, int) else None,
        "instructions": sum(map(estimate_tokens, system)) if system else None,
        "tool_schemas": {
            tool.get("name") or f"#{index}": estimate_tokens(json.dumps(tool))
            for index, tool in enumerate(tools)
            if isinstance(tool, dict)
        },
    }


def session_use(session: sessions.SelectedSession) -> dict:
    """The tools and skills the part read used, and its first request."""
    tools: Counter = Counter()
    skills: Counter = Counter()
    for entry in session.entries():
        for block in entry.content:
            name = block.get("name")
            if block.get("type") != "tool_use" or not name:
                continue
            tools[name] += 1
            given = block.get("input")
            if name == SKILL_TOOL and isinstance(given, dict):
                if isinstance(given.get("skill"), str):
                    skills[given["skill"]] += 1
    return {
        "tools": dict(sorted(tools.items())),
        "skills": dict(sorted(skills.items())),
        "first_request": first_request(session.document),
    }


def mcp_name(tool: str) -> tuple[str, str] | None:
    """The server and tool of an MCP tool as Claude Code names it,
    `mcp__<server>__<tool>`."""
    if not tool.startswith("mcp__"):
        return None
    server, _, name = tool[len("mcp__") :].partition("__")
    return (server, name) if server and name else None


# Configuration files


def _shown(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def _matches(patterns: tuple[str, ...]) -> list[Path]:
    found = []
    for pattern in patterns:
        found += [Path(p) for p in sorted(glob.glob(str(Path(pattern).expanduser())))]
    return [path for path in found if path.is_file()]


def _servers(parsed: object) -> set[str]:
    """MCP server names in a parsed configuration: `mcpServers` (Claude Code,
    Kiro, LM Studio, and Claude Code's per-project entries) or `mcp_servers`
    (Codex)."""
    if not isinstance(parsed, dict):
        return set()
    tables = [parsed.get("mcpServers"), parsed.get("mcp_servers")]
    projects = parsed.get("projects")
    if isinstance(projects, dict):
        tables += [
            p.get("mcpServers") for p in projects.values() if isinstance(p, dict)
        ]
    return {name for table in tables if isinstance(table, dict) for name in table}


def configured(files: HarnessFiles, problems: list[str]) -> list[str]:
    names: set[str] = set()
    for raw in files.mcp:
        path = Path(raw).expanduser()
        if not path.is_file():
            continue
        try:
            text = path.read_text()
            parsed = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        except (OSError, ValueError) as exc:
            problems.append(f"{raw}: {exc}")
            continue
        names |= _servers(parsed)
    return sorted(names)


def _frontmatter(text: str) -> str:
    """A skill file's front matter, which is all a skill listing loads."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[: end + 4] if end != -1 else ""


def _sized(paths: list[Path], problems: list[str], *, listing: bool) -> list[tuple]:
    sized = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            problems.append(f"{_shown(path)}: {exc}")
            continue
        if listing:
            sized.append(
                (f"skill {path.parent.name}", estimate_tokens(_frontmatter(text)))
            )
        else:
            sized.append((_shown(path), estimate_tokens(text)))
    return sized


# The run's inventory


def _read(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """The sessions the run read, as recorded with what each used; forgotten
    ones are left out (2.11)."""
    return conn.execute(
        "SELECT s.* FROM steps t JOIN sessions_seen s"
        " ON s.source = json_extract(t.input_ref, '$.source')"
        " AND s.session_id = json_extract(t.input_ref, '$.id')"
        " WHERE t.run = ? AND t.phase = 'read' AND t.state = 'done'"
        " AND s.inventory IS NOT NULL AND s.forgotten = 0 ORDER BY t.seq",
        (run_id,),
    ).fetchall()


def _needs(conn: sqlite3.Connection, cfg: dict, run_id: str) -> list[dict]:
    """What the run observed under the tool-use lens: tools that failed, and
    needs a configured or available tool could have met (8.1)."""
    chars = cfg["thresholds"]["quote_chars"]
    rows = conn.execute(
        f"SELECT * FROM ({knowledge.SHOWN_OBSERVATIONS}) WHERE run = ? AND lens = ?"
        " ORDER BY observed_on, evidence_id",
        (run_id, NEEDS_LENS),
    )
    return [
        {
            "session": f"{row['source']}:{row['session_id']}",
            "quote": row["quote"][:chars],
            "key": row["pattern_key"],
        }
        for row in rows
    ]


def _harness(used: list[dict], configured_here: list[str], shared: list) -> dict:
    tools: Counter = Counter()
    skills: Counter = Counter()
    servers: dict[str, Counter] = {}
    for found in used:
        skills.update(found["skills"])
        for tool, count in found["tools"].items():
            named = mcp_name(tool)
            if named is None:
                tools[tool] += count
            else:
                servers.setdefault(named[0], Counter())[named[1]] += count
    return {
        "configured": configured_here,
        "available": sorted(set(shared) - set(configured_here)),
        "servers": {s: dict(sorted(c.items())) for s, c in sorted(servers.items())},
        "tools": dict(sorted(tools.items())),
        "skills": dict(sorted(skills.items())),
        "never_used": sorted(set(configured_here) - set(servers)),
        "sessions": len(used),
    }


def _part(label: str, tokens: int | None = None, source: str | None = None) -> dict:
    return {"label": label, "tokens": tokens, "from": source}


UNAVAILABLE = _part("unavailable")


def _window(cfg: dict, model: str | None) -> int | None:
    """The context of a configured backend running this model, if any."""
    if not model:
        return None
    known = [
        backend["context_tokens"]
        for backend in cfg["backends"].values()
        if backend["model"] and backend["model"].lower() == model.lower()
    ]
    return min(known) if known else None


def _weight(first: Mapping) -> int:
    if first["measured"] is not None:
        return first["measured"]
    return (first["instructions"] or 0) + sum(first["tool_schemas"].values())


def _context_row(cfg, harness, model, session, instruction_files, skill_listings):
    first = session["first_request"] if session else None
    contributors: list[tuple] = []
    if first and first["instructions"] is not None:
        instructions = _part(
            "estimated", first["instructions"], "the session's system text"
        )
        contributors.append(("system text", first["instructions"]))
    elif instruction_files:
        total = sum(tokens for _, tokens in instruction_files)
        instructions = _part("estimated", total, "instruction files")
        contributors += instruction_files
    else:
        instructions = UNAVAILABLE
    skills = UNAVAILABLE
    if skill_listings:
        total = sum(tokens for _, tokens in skill_listings)
        skills = _part("estimated", total, "skill listings")
        contributors += skill_listings
    schemas = UNAVAILABLE
    if first and first["tool_schemas"]:
        total = sum(first["tool_schemas"].values())
        schemas = _part("estimated", total, "the first request's tool schemas")
        contributors += [(f"tool {n}", t) for n, t in first["tool_schemas"].items()]
    measured = UNAVAILABLE
    if first and first["measured"] is not None:
        measured = _part("measured", first["measured"], "the first turn's input tokens")
    estimated = [p["tokens"] for p in (instructions, skills, schemas) if p["tokens"]]
    if measured["tokens"] is not None:
        total = {"label": "measured", "tokens": measured["tokens"]}
    elif estimated:
        total = {"label": "estimated", "tokens": sum(estimated)}
    else:
        total = {"label": "unavailable", "tokens": None}
    window = _window(cfg, model)
    share = cfg["thresholds"]["context_weight_share"]
    largest = sorted(contributors, key=lambda c: (-c[1], c[0]))[:CONTRIBUTORS]
    return {
        "harness": harness,
        "model": model,
        "session": session["ref"] if session else None,
        "instructions": instructions,
        "skills": skills,
        "tool_schemas": schemas,
        "measured": measured,
        "total": total,
        "window": window,
        "flagged": bool(
            window and total["tokens"] and total["tokens"] > share * window
        ),
        "contributors": [{"name": name, "tokens": tokens} for name, tokens in largest],
    }


def build(conn: sqlite3.Connection, cfg: dict, run_id: str) -> dict:
    problems: list[str] = []
    try:
        shared = sorted(mcp.load_servers(cfg))
    except DreamingError as exc:
        problems.append(exc.message)
        shared = []
    read = [
        {
            "ref": f"{row['source']}:{row['session_id']}",
            "harness": row["harness"] or row["source"],
            "model": row["model"],
            **json.loads(row["inventory"]),
        }
        for row in _read(conn, run_id)
    ]
    present = {
        name
        for name, files in HARNESS_FILES.items()
        if any(Path(raw).expanduser().is_file() for raw in files.mcp)
        or _matches(files.instructions + files.skills)
    }
    names = sorted(present | {found["harness"] for found in read})
    harnesses, context = {}, []
    for name in names:
        files = HARNESS_FILES.get(name, HarnessFiles((), (), ()))
        used = [found for found in read if found["harness"] == name]
        harnesses[name] = _harness(used, configured(files, problems), shared)
        instruction_files = _sized(
            _matches(files.instructions), problems, listing=False
        )
        skill_listings = _sized(_matches(files.skills), problems, listing=True)
        heaviest: dict[str | None, dict | None] = {}
        for found in used:
            kept = heaviest.get(found["model"])
            if kept is None or _weight(found["first_request"]) > _weight(
                kept["first_request"]
            ):
                heaviest[found["model"]] = found
        for model, session in sorted(heaviest.items(), key=lambda i: i[0] or "") or [
            (None, None)
        ]:
            context.append(
                _context_row(
                    cfg, name, model, session, instruction_files, skill_listings
                )
            )
    return {
        "harnesses": harnesses,
        "needs": _needs(conn, cfg, run_id),
        "context": context,
        "share": cfg["thresholds"]["context_weight_share"],
        "problems": problems,
    }


def record(conn: sqlite3.Connection, cfg: dict, run_id: str) -> dict:
    """Build the run's inventory and store it with the run."""
    found = build(conn, cfg, run_id)
    conn.execute(
        "UPDATE runs SET inventory = ? WHERE id = ?", (json.dumps(found), run_id)
    )
    return found


def load(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT inventory FROM runs WHERE id = ?", (run_id,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None

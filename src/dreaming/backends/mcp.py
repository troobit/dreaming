"""Per-call MCP servers from the shared definitions (4.4, 4.6, 5.7).

A role lists MCP tools as `<server>.<tool>`. A call gets only the servers its
role names, each limited to the named tools, so every harness, including one
served by LM Studio, receives the same filtered set. devtools enables its
optional tools through `ENABLE_ADDITIONAL_TOOLS`; that list is cut to the role's
tools, which never include an agent-launching tool, so `codex-agent` and
`gemini-agent` are never started.

Secrets are never written into a call's configuration. As in agentic-coding's
generated configs, a server's secret is read from the environment variable
`<SERVER>_<SECRET>` (for example `GITHUB_AUTH_TOKEN` for the GitHub server's
Authorization header), referenced in each harness's own way.

The shared definitions are optional (14.4): without the file, or without a
server a role names, the role's calls go on without that server's tools, and
the report lists them (`unavailable_tools`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from dreaming.config import AGENT_LAUNCH_TOOLS, BUILTIN_TOOLS
from dreaming.errors import USAGE_ERROR, DreamingError

ADDITIONAL_TOOLS = "ENABLE_ADDITIONAL_TOOLS"


def _invalid(message: str) -> DreamingError:
    return DreamingError(message, USAGE_ERROR, "invalid_config")


def load_servers(cfg: dict) -> dict:
    """The shared definitions named by `mcp.servers_file`, or none when it is
    not set or does not exist."""
    raw = cfg["mcp"]["servers_file"]
    if not raw:
        return {}
    path = Path(raw).expanduser()
    if not path.exists():
        return {}
    try:
        servers = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _invalid(f"mcp.servers_file {path}: {exc}") from exc
    if not isinstance(servers, dict):
        raise _invalid(f"mcp.servers_file {path}: must be a JSON object of servers")
    return servers


def mcp_tools(tools: list[str]) -> dict[str, list[str]]:
    """The role's MCP tools by server, in the order listed."""
    by_server: dict[str, list[str]] = {}
    for tool in tools:
        if tool not in BUILTIN_TOOLS:
            server, _, name = tool.partition(".")
            by_server.setdefault(server, []).append(name)
    return by_server


def secret_var(server: str, secret: str) -> str:
    """The environment variable holding a server's secret, named as agentic-coding
    names it."""
    return f"{server}_{secret}".upper().replace("-", "_")


def _defined(servers: dict, server: str) -> bool:
    return isinstance((servers.get(server) or {}).get("transport"), dict)


def unavailable_tools(cfg: dict) -> list[dict]:
    """Each MCP tool a role lists that the shared definitions cannot supply,
    with the reason; the role's calls go on without it (14.4)."""
    servers = load_servers(cfg)
    raw = cfg["mcp"]["servers_file"]
    missing = []
    for role, tools in cfg["tools"].items():
        for server, names in mcp_tools(tools).items():
            if _defined(servers, server):
                continue
            if not raw:
                reason = "no shared MCP definitions are configured (mcp.servers_file)"
            elif not Path(raw).expanduser().exists():
                reason = f"the shared MCP definitions {raw} do not exist"
            else:
                reason = f"the shared MCP definitions have no {server} server"
            missing += [
                {"role": role, "tool": f"{server}.{name}", "reason": reason}
                for name in names
            ]
    return missing


def role_servers(servers: dict, tools: list[str]) -> dict[str, dict]:
    """The definition of each server the role's tools name that the shared
    definitions have, keyed by server, with devtools' optional tools cut to the
    role's."""
    chosen = {}
    for server, names in mcp_tools(tools).items():
        if not _defined(servers, server):
            continue
        definition = copy.deepcopy(servers[server])
        env = definition["transport"].get("env") or {}
        if ADDITIONAL_TOOLS in env:
            enabled = env[ADDITIONAL_TOOLS].split(",")
            env[ADDITIONAL_TOOLS] = ",".join(
                tool
                for tool in enabled
                if tool in names and tool not in AGENT_LAUNCH_TOOLS
            )
        chosen[server] = definition
    return chosen


def _secrets(server: str, definition: dict) -> list[tuple[str, dict]]:
    """`(variable, mechanism)` for each secret; a mechanism names a header or an
    environment variable."""
    found = []
    for secret, mechanism in sorted((definition.get("secrets") or {}).items()):
        if "header" not in mechanism and "env" not in mechanism:
            raise _invalid(
                f"MCP server {server}: secret {secret} names neither a header nor "
                "an environment variable"
            )
        found.append((secret_var(server, secret), mechanism))
    return found


def claude_config(servers: dict, tools: list[str]) -> dict:
    """The `--mcp-config` value for a call. Secrets are `${VAR}` references, which
    Claude Code expands from its environment."""
    rendered = {}
    for server, definition in role_servers(servers, tools).items():
        entry = definition["transport"]
        for var, mechanism in _secrets(server, definition):
            if "header" in mechanism:
                entry.setdefault("headers", {})[mechanism["header"]] = f"${{{var}}}"
            else:
                entry.setdefault("env", {})[mechanism["env"]] = f"${{{var}}}"
        rendered[server] = entry
    return {"mcpServers": rendered}


def codex_config(servers: dict, tools: list[str]) -> dict:
    """The `mcp_servers` table for a call, each server limited to the role's tools.
    Header secrets become `env_http_headers`; an environment secret is forwarded
    with `env_vars`, which keeps a variable's name, so the server must expect
    that name."""
    names = mcp_tools(tools)
    rendered = {}
    for server, definition in role_servers(servers, tools).items():
        entry = {k: v for k, v in definition["transport"].items() if k != "type"}
        entry["enabled_tools"] = names[server]
        for var, mechanism in _secrets(server, definition):
            if "header" in mechanism:
                entry.setdefault("env_http_headers", {})[mechanism["header"]] = var
            elif mechanism["env"] == var:
                entry.setdefault("env_vars", []).append(var)
            else:
                raise _invalid(
                    f"MCP server {server}: Codex can pass the secret only as {var}, "
                    f"but the server expects {mechanism['env']}"
                )
        rendered[server] = entry
    return rendered

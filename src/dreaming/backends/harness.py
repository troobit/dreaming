"""Shared per-call directories, MCP filtering and event accounting for CLI adapters."""

import copy
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from dreaming.backends.probe import _events, _payload, _usage, probe_backend
from dreaming.backends.process import run_process
from dreaming.config import TOOL_FREE, BackendConfig, agent_tool, ensure_home, require_external
from dreaming.redact import redact
from dreaming.steps import CallResult

BUILTINS = {"Read", "Edit", "Write", "Glob", "Grep"}


def check_tools(role: str, tools: list[str]) -> None:
    if any(agent_tool(t) for t in tools):
        raise ValueError("Role cannot expose an agent-launch tool")
    if role in TOOL_FREE and tools:
        raise ValueError(f"{role} must be tool-free")


def filtered_servers(definitions: dict, tools: list[str]) -> tuple[dict, list[str]]:
    """Accept shared registry transport records or ordinary mcpServers definitions."""
    selected: dict[str, list[str]] = {}
    for tool in tools:
        if tool in BUILTINS:
            continue
        if tool.startswith("mcp__"):
            _, server, name = tool.split("__", 2)
        else:
            server, name = "devtools", tool
        selected.setdefault(server, []).append(name)
    result, names = {}, []
    for server, allowed in selected.items():
        if server not in definitions:
            raise ValueError(f"Missing shared MCP definition for {server}")
        record = copy.deepcopy(definitions[server])
        transport = record.get("transport", record)
        if transport.get("type") == "stdio":
            transport.pop("type")
        if server == "devtools":
            transport.setdefault("env", {})["ENABLE_ADDITIONAL_TOOLS"] = ",".join(allowed)
        result[server] = transport
        names.extend(f"mcp__{server}__{name}" for name in allowed)
    return result, names


class HarnessBackend:
    supports_tools = True
    harness: str

    def __init__(
        self,
        name: str,
        config: BackendConfig,
        home: Path,
        run_id: str,
        *,
        capabilities=None,
        servers: dict | Path | None = None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("Invalid run id")
        self.name, self.config, self.home, self.run_id = name, config, ensure_home(home), run_id
        self.context_tokens, self.capabilities = config.context_tokens, capabilities
        if isinstance(servers, Path):
            servers = json.loads(servers.read_text())
        self.servers = (servers or {}).get("mcpServers", servers or {})
        self.session_ref = None
        self.budget = None

    def probe(self):
        if self.capabilities is None:
            limits = {}
            if self.budget is not None:
                limits = {
                    "max_tokens": self.budget.limits.max_tokens,
                    "max_seconds": self.budget.limits.max_seconds - self.budget.seconds_used,
                }
            self.capabilities = probe_backend(
                self.name, self.config, self.home, run_id=self.run_id, **limits
            )
        return self.capabilities

    def new_session(self):
        self.session_ref = str(uuid.uuid4())
        return self.session_ref if self.harness == "claude" else None

    def call(self, role: str, prompt: str, schema: dict, tools: list[str]) -> CallResult:
        check_tools(role, tools)
        probe = self.probe()
        if not probe.compatible:
            return CallResult("", outcome="transport_failure", error=probe.reason)
        if self.harness == "codex" and role in TOOL_FREE and probe.tools:
            return CallResult(
                "",
                outcome="transport_failure",
                error="Codex probe cannot guarantee a tool-free role",
            )
        if self.harness == "codex" and set(probe.tools) - set(probe.agent_tools) - set(tools):
            return CallResult(
                "",
                outcome="transport_failure",
                error="Codex probe exposes tools outside the role allowlist",
            )
        if self.session_ref is None:
            self.new_session()
        call_id = self.session_ref
        cwd = require_external(self.home / "runs" / self.run_id / "calls" / call_id)
        cwd.mkdir(parents=True, mode=0o700)
        definitions, mcp_tools = filtered_servers(self.servers, tools)
        for server, definition in list(definitions.items()):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", server) or "command" not in definition:
                raise ValueError("Role tool filtering requires a named stdio MCP server")
            selected = [
                name.split("__", 2)[2] for name in mcp_tools if name.startswith(f"mcp__{server}__")
            ]
            proxy_config = cwd / f"server-{server}.json"
            proxy_config.write_text(json.dumps({"server": definition, "allowed_tools": selected}))
            proxy_config.chmod(0o600)
            definitions[server] = {
                "command": sys.executable,
                "args": ["-m", "dreaming.backends.mcp_filter", str(proxy_config)],
                "env": definition.get("env", {}),
            }
        mcp_path = cwd / "mcp.json"
        mcp_path.write_text(json.dumps({"mcpServers": definitions}))
        mcp_path.chmod(0o600)
        schema_path, final = cwd / "schema.json", cwd / "final.json"
        schema_path.write_text(json.dumps(schema))
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        env.update(
            CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
            CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
            ENABLE_ADDITIONAL_TOOLS="",
        )
        command = self.command(
            role, tools, mcp_tools, definitions, mcp_path, schema_path, final, env
        )
        if not probe.structured_output:
            prompt += "\n\nReturn only JSON matching this schema:\n" + json.dumps(schema)
        started = time.monotonic()
        try:
            process = run_process(command, cwd, env, self.config.timeout_seconds, input_text=prompt)
        except OSError as exc:
            self.session_ref = None
            return CallResult(
                "", seconds=time.monotonic() - started, outcome="transport_failure", error=str(exc)
            )
        events = _events(process.stdout)
        session_ref = call_id if self.harness == "claude" else None
        for event in events:
            if event.get("type") == "thread.started":
                session_ref = event.get("thread_id")
            elif isinstance(event.get("session_id"), str):
                session_ref = event["session_id"]
        if self.harness == "codex":
            for directory in (self.home / "runs" / self.run_id / "codex", self.home / "codex-exec"):
                directory = require_external(directory)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = directory / f"{call_id}.jsonl"
                path.write_text(redact(process.stdout))
                path.chmod(0o600)
        incoming, outgoing, estimated = _usage(events, prompt, process.stdout)
        model = next(
            (event["model"] for event in reversed(events) if isinstance(event.get("model"), str)),
            self.config.model,
        )
        raw, outcome, error = process.stdout, "success", None
        failures = [
            event
            for event in events
            if event.get("is_error") or event.get("type") in {"error", "turn.failed"}
        ]
        if process.returncode or process.reason or failures:
            outcome = "transport_failure"
            error = process.reason or "\n".join(
                part
                for part in (
                    process.stderr,
                    json.dumps(failures) if failures else "",
                    process.stdout,
                )
                if part
            )
        else:
            try:
                raw = json.dumps(_payload(events, final))
            except (OSError, ValueError, KeyError, TypeError):
                # Keep malformed final text for the dispatcher's schema retry policy.
                raw = final.read_text() if final.is_file() else process.stdout
        self.session_ref = None
        return CallResult(
            raw,
            incoming,
            outgoing,
            time.monotonic() - started,
            estimated,
            session_ref,
            outcome,
            error,
            model,
        )

"""Codex as a role harness: command lines, prompt inspection and event parsing."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dreaming.backends import (
    BackendError,
    CallResult,
    call_dir,
    mcp,
    parse_reply,
    run_process,
)

AGENT_TOOLS = ("spawn_agent",)
WRITE_TOOLS = ("edit", "write")

# Options that suppress user configuration Codex would otherwise put in context.
# The user's MCP servers are replaced by `mcp_servers=...` on every command.
SUPPRESS = (
    "-c",
    "skills.include_instructions=false",
    "-c",
    "project_doc_max_bytes=0",
    "--disable",
    "plugins",
    "--disable",
    "apps",
)

# Markers of user configuration in `codex debug prompt-input` output.
VIEW_LEAKS = {
    "instructions": "# AGENTS.md instructions",
    "skills": "### Available skills",
    "mcp": "Deferred tool namespaces",
    "agent_tool": "spawn_agent",
}


def user_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def isolated_home(home: Path) -> Path:
    """An empty `CODEX_HOME` that keeps the user's login through a link to auth.json."""
    isolated = home / "codex-home"
    isolated.mkdir(exist_ok=True)
    auth, link = user_codex_home() / "auth.json", isolated / "auth.json"
    if auth.exists() and not link.is_symlink():
        link.symlink_to(auth)
    return isolated


def toml_value(value: object) -> str:
    """A JSON-like value as a TOML inline value, for `-c key=value` overrides."""
    if isinstance(value, dict):
        items = (f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items())
        return "{" + ", ".join(items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def provider_args(backend: dict) -> list[str]:
    args = []
    if backend.get("provider") == "lmstudio":
        args += ["--oss", "--local-provider", "lmstudio"]
    if backend.get("model"):
        args += ["-m", backend["model"]]
    return args


def prompt_view_command(prompt: str) -> list[str]:
    """Renders the model-visible prompt without calling a model."""
    return ["codex", "debug", "prompt-input", *SUPPRESS, "-c", "mcp_servers={}", prompt]


def exec_command(
    backend: dict,
    *,
    cwd: Path,
    schema_path: Path | None,
    servers: dict | None = None,
    sandbox: str = "read-only",
) -> list[str]:
    """The `codex exec --json` command line; the prompt is sent on stdin. Codex
    never asks for approval, so no prompt can block a call."""
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "-C",
        str(cwd),
        *SUPPRESS,
        "-c",
        f"mcp_servers={toml_value(servers or {})}",
        "-c",
        'approval_policy="never"',
        *provider_args(backend),
    ]
    if schema_path is not None:
        argv += ["--output-schema", str(schema_path)]
    return [*argv, "-"]


def view_leaks(stdout: str, canary: str) -> list[str]:
    """Leak names found in a prompt view; a canary means project instructions loaded."""
    leaks = [name for name, marker in VIEW_LEAKS.items() if marker in stdout]
    if canary in stdout and "instructions" not in leaks:
        leaks.insert(0, "instructions")
    return leaks


@dataclass
class ExecEvents:
    thread_id: str = ""
    message: str | None = None
    usage: dict | None = None
    error: str = ""
    agent_events: list[dict] = field(default_factory=list)


def _event(line: str) -> dict:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def _launches_agent(event: dict) -> bool:
    item = event.get("item") or {}
    return str(event.get("type", "")).startswith("item.") and (
        item.get("type") == "collab_tool_call"
        or item.get("name") in AGENT_TOOLS
        or item.get("tool") in AGENT_TOOLS
    )


def launches_agent(line: str) -> bool:
    """Whether one line of an exec stream starts or reports another agent."""
    return _launches_agent(_event(line))


def parse_events(stdout: str) -> ExecEvents:
    """Thread, final message, usage, failure and agent launches of an exec stream."""
    events = ExecEvents()
    for line in stdout.splitlines():
        event = _event(line)
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started" and not events.thread_id:
            events.thread_id = str(event.get("thread_id") or "")
        elif kind == "item.completed" and item.get("type") == "agent_message":
            events.message = item.get("text")
        elif kind == "turn.completed":
            events.usage = event.get("usage")
        elif kind in ("turn.failed", "error"):
            error = event.get("error") or event
            events.error = str(error.get("message") or error)
        if _launches_agent(event):
            events.agent_events.append(item)
    return events


def usage(events: ExecEvents) -> tuple[int, int] | None:
    """Tokens in (cached input is a subset of input) and out, if reported."""
    if not isinstance(events.usage, dict):
        return None
    reported = events.usage
    return int(reported.get("input_tokens") or 0), int(
        reported.get("output_tokens") or 0
    )


class CodexBackend:
    """Calls through `codex exec --json` with the isolation its probe confirmed.

    Codex cannot remove its agent-launch tool (Q81), so a call is killed as soon
    as its event stream shows another agent starting (4.4).
    """

    accepts_session_id = False

    def __init__(
        self,
        cfg: dict,
        name: str,
        *,
        home: Path,
        run_id: str,
        isolation: str = "user-home",
    ) -> None:
        self.cfg = cfg
        self.name = name
        self.entry = cfg["backends"][name]
        self.context_tokens = self.entry["context_tokens"]
        self.release_date = self.entry["release_date"]
        self.home = home
        self.run_id = run_id
        self.isolation = isolation
        self.servers = mcp.load_servers(cfg)

    def probe(self):
        from dreaming.backends import probe  # probe imports this module

        return probe.probe_backend(self.home, self.cfg, self.name)

    def call(
        self,
        role: str,
        prompt: str,
        schema: dict,
        tools: list[str],
        session_id: str | None = None,
    ) -> CallResult:
        # Streams sit where apsis reads Codex exec streams (`runs/`, 5.8).
        streams = self.home / "runs" / self.run_id / "codex"
        schema_text = json.dumps(schema, sort_keys=True)
        schema_path = streams / "schemas" / f"{_digest(schema_text)}.json"
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(schema_text)
        cwd = call_dir(self.home, self.run_id)
        env = dict(os.environ)
        if self.isolation == "isolated-home":
            env["CODEX_HOME"] = str(isolated_home(self.home))
        writes = any(tool in WRITE_TOOLS for tool in tools)
        argv = exec_command(
            self.entry,
            cwd=cwd,
            schema_path=schema_path,
            servers=mcp.codex_config(self.servers, tools),
            sandbox="workspace-write" if writes else "read-only",
        )
        timeout = self.cfg["thresholds"]["call_timeout_seconds"]
        proc = run_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stdin_text=prompt,
            stop_when=launches_agent,
        )
        events = parse_events(proc.stdout)
        (streams / f"{events.thread_id or uuid.uuid4()}.jsonl").write_text(proc.stdout)
        if proc.stopped:
            raise BackendError(
                f"{self.name} started another agent, so the call was killed (4.4)"
            )
        if proc.timed_out:
            raise BackendError(f"{self.name}: no reply within {timeout}s; killed")
        if proc.returncode != 0 or events.error or events.message is None:
            details = [events.error, proc.stderr.strip(), f"exit {proc.returncode}"]
            raise BackendError(" | ".join(d for d in details if d))
        tokens_in, tokens_out = usage(events) or (None, None)
        return CallResult(
            parse_reply(events.message),
            tokens_in,
            tokens_out,
            session_ref=events.thread_id,
            model=self.entry["model"] or None,
        )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]

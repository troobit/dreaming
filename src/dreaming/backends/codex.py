"""Codex as a role harness: command lines, prompt inspection and event parsing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

AGENT_TOOLS = ("spawn_agent",)

# Options that suppress user configuration Codex would otherwise put in context.
SUPPRESS = (
    "-c",
    "skills.include_instructions=false",
    "-c",
    "project_doc_max_bytes=0",
    "-c",
    "mcp_servers={}",
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


def provider_args(backend: dict) -> list[str]:
    args = []
    if backend.get("provider") == "lmstudio":
        args += ["--oss", "--local-provider", "lmstudio"]
    if backend.get("model"):
        args += ["-m", backend["model"]]
    return args


def prompt_view_command(prompt: str) -> list[str]:
    """Renders the model-visible prompt without calling a model."""
    return ["codex", "debug", "prompt-input", *SUPPRESS, prompt]


def exec_command(backend: dict, *, cwd: Path, schema_path: Path | None) -> list[str]:
    """The `codex exec --json` command line; the prompt is sent on stdin."""
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-C",
        str(cwd),
        *SUPPRESS,
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


def parse_events(stdout: str) -> ExecEvents:
    """Thread, final message, usage, failure and agent launches of an exec stream."""
    events = ExecEvents()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
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
        if kind and kind.startswith("item.") and _launches_agent(item):
            events.agent_events.append(item)
    return events


def _launches_agent(item: dict) -> bool:
    return item.get("type") == "collab_tool_call" or item.get("name") in AGENT_TOOLS


def usage(events: ExecEvents) -> tuple[int, int] | None:
    """Tokens in (cached input is a subset of input) and out, if reported."""
    if not isinstance(events.usage, dict):
        return None
    reported = events.usage
    return int(reported.get("input_tokens") or 0), int(
        reported.get("output_tokens") or 0
    )

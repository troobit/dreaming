"""Claude Code as a role harness: command line, isolation and stream parsing."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

AGENT_TOOLS = ("Agent", "Task")
DEFAULT_LMSTUDIO_URL = "http://localhost:1234"


@dataclass(frozen=True)
class Isolation:
    """A set of flags and environment that keeps user and project configuration out."""

    name: str
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


# In order of preference (design, Roles and calls).
ISOLATIONS = (
    Isolation("safe-mode", ("--safe-mode",)),
    Isolation(
        "setting-sources",
        ("--setting-sources", "", "--disable-slash-commands"),
        {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
    ),
)


def command(
    backend: dict,
    *,
    isolation: Isolation,
    session_id: str,
    tools: list[str],
    mcp_config: dict,
    schema: dict | None,
) -> list[str]:
    """The `claude -p` command line; the prompt is sent on stdin."""
    argv = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        session_id,
        "--permission-mode",
        "dontAsk",
        *isolation.args,
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp_config),
        "--tools",
        ",".join(tools),
    ]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema)]
    if backend.get("model"):
        argv += ["--model", backend["model"]]
    return argv


def environment(backend: dict, isolation: Isolation) -> dict[str, str]:
    env = {**os.environ, **isolation.env}
    if backend.get("provider") == "lmstudio":
        env["ANTHROPIC_BASE_URL"] = backend.get("base_url") or DEFAULT_LMSTUDIO_URL
        env["ANTHROPIC_AUTH_TOKEN"] = "lmstudio"
    return env


def parse_stream(stdout: str) -> tuple[dict, dict | None]:
    """The `init` system event and the final `result` event of a stream-json run."""
    init: dict = {}
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
    return init, result


def usage(result: dict) -> tuple[int, int] | None:
    """Tokens in (including cache reads and writes) and out, if reported."""
    reported = result.get("usage")
    if not isinstance(reported, dict):
        return None
    tokens_in = sum(
        int(reported.get(key) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    return tokens_in, int(reported.get("output_tokens") or 0)

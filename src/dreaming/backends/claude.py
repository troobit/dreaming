"""Claude Code as a role harness: command line, isolation and stream parsing."""

from __future__ import annotations

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
from dreaming.config import BUILTIN_TOOLS

AGENT_TOOLS = ("Agent", "Task")
DEFAULT_LMSTUDIO_URL = "http://localhost:1234"
TOOL_NAMES = {
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "edit": "Edit",
    "write": "Write",
}


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
    allowed_tools: list[str] = (),
    add_dirs: list[str] = (),
) -> list[str]:
    """The `claude -p` command line; the prompt is sent on stdin, so the flags
    that take several values cannot swallow it.

    `--tools` is the complete list of built-in tools the model sees, and
    `--allowedTools` approves the role's tools in advance; with `dontAsk`
    anything else is refused rather than prompted for.
    """
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
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if add_dirs:
        argv += ["--add-dir", *add_dirs]
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


def readable_dirs(cfg: dict) -> list[str]:
    """Target repositories and the knowledge folders that exist, which file tools
    may read outside the call's working directory (4.6). A missing knowledge
    folder is skipped."""
    paths = [repo["path"] for repo in cfg["repositories"].values()]
    paths += cfg["knowledge_folders"]
    found = (Path(path).expanduser() for path in paths)
    return [str(path) for path in found if path.is_dir()]


class ClaudeBackend:
    """Calls through `claude -p` with the isolation its probe confirmed."""

    accepts_session_id = True

    def __init__(
        self,
        cfg: dict,
        name: str,
        *,
        home: Path,
        run_id: str,
        isolation: str = ISOLATIONS[0].name,
    ) -> None:
        self.cfg = cfg
        self.name = name
        self.entry = cfg["backends"][name]
        self.context_tokens = self.entry["context_tokens"]
        self.release_date = self.entry["release_date"]
        self.home = home
        self.run_id = run_id
        self.isolation = next(i for i in ISOLATIONS if i.name == isolation)
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
        session_id = session_id or str(uuid.uuid4())
        builtin = [TOOL_NAMES[tool] for tool in tools if tool in BUILTIN_TOOLS]
        allowed = builtin + [
            f"mcp__{server}__{tool}"
            for server, names in mcp.mcp_tools(tools).items()
            for tool in names
        ]
        argv = command(
            self.entry,
            isolation=self.isolation,
            session_id=session_id,
            tools=builtin,
            mcp_config=mcp.claude_config(self.servers, tools),
            schema=schema,
            allowed_tools=allowed,
            add_dirs=readable_dirs(self.cfg) if builtin else [],
        )
        timeout = self.cfg["thresholds"]["call_timeout_seconds"]
        proc = run_process(
            argv,
            cwd=call_dir(self.home, self.run_id),
            env=environment(self.entry, self.isolation),
            timeout=timeout,
            stdin_text=prompt,
        )
        if proc.timed_out:
            raise BackendError(f"{self.name}: no reply within {timeout}s; killed")
        init, final = parse_stream(proc.stdout)
        if proc.returncode != 0 or final is None or final.get("is_error"):
            details = [(final or {}).get("result"), proc.stderr.strip()]
            details.append(f"exit {proc.returncode}")
            raise BackendError(" | ".join(str(d) for d in details if d))
        output = final.get("structured_output")
        if output is None:
            output = parse_reply(final.get("result"))
        tokens_in, tokens_out = usage(final) or (None, None)
        return CallResult(
            output,
            tokens_in,
            tokens_out,
            session_ref=session_id,
            model=init.get("model") or self.entry["model"] or None,
        )

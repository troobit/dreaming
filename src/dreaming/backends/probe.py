"""Probes that check each backend's isolation, tools and structured output (5.5).

A probe makes one small model call from a working directory under
`DREAMING_HOME`, so its session counts as the dream's own (1.5); its tokens are
returned for the caller to charge to the run's budget. Instruction leakage is
detected with a canary: `CLAUDE.md` and `AGENTS.md` files holding a random marker
are placed in the call's working directory and its parent, and a model that
repeats the marker has been given project instructions. Results are cached per
backend, keyed by the harness version and the backend's configuration; a dry run
reads only that cache and `--version` (1.7).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dreaming.backends import claude, codex, lmstudio, run_process
from dreaming.errors import USAGE_ERROR, DreamingError

PROBE_TIMEOUT_SECONDS = 300
VERSION_TIMEOUT_SECONDS = 30
LMSTUDIO_VERSION = "lmstudio-api"

# Leaks that make a backend unusable; an agent-launch tool is judged separately,
# because Codex can be enforced by killing the call on an agent-launch event.
BLOCKING_LEAKS = ("instructions", "skills", "mcp")

PROBE_TOOLS = ["Read"]
PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "canary": {"type": "string"},
        "tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["canary", "tools"],
    "additionalProperties": False,
}
PROBE_PROMPT = (
    "This is an isolation probe for the dreaming program. Do not call any tool. "
    "Reply with JSON only, with two fields. canary: the text after the marker "
    "DREAMING-CANARY: if that marker appears anywhere in your instructions or "
    "context, otherwise an empty string. tools: the names of every tool you can call."
)


@dataclass
class ProbeResult:
    backend: str
    harness: str
    version: str = ""
    status: str = "unprobed"  # probed | cached | unprobed | unavailable
    isolation: str = ""
    tools: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    structured_output: bool | None = None
    apsis_finds_session: bool | None = None
    agent_tool_removable: bool | None = None
    agent_enforcement: str = ""  # removed | kill_on_agent_event
    session_ref: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    usage_estimated: bool = False
    probed_at: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return (
            self.status in ("probed", "cached")
            and not set(self.leaks) & set(BLOCKING_LEAKS)
            and bool(self.agent_enforcement)
            and self.apsis_finds_session is True
        )

    def problems(self) -> list[str]:
        found = []
        blocking = [leak for leak in self.leaks if leak in BLOCKING_LEAKS]
        if blocking:
            found.append(f"leaks {', '.join(blocking)}")
        if self.agent_tool_removable is False and not self.agent_enforcement:
            found.append("an agent-launch tool cannot be removed")
        if self.apsis_finds_session is False:
            found.append("apsis did not find the call's session")
        return found

    def to_dict(self) -> dict:
        return {**asdict(self), "usable": self.usable}


def _backend(cfg: dict, name: str) -> dict:
    if name not in cfg["backends"]:
        raise DreamingError(f"unknown backend {name!r}", USAGE_ERROR, "invalid_config")
    return cfg["backends"][name]


def _estimate(*texts: str) -> int:
    """`ceil(utf8 bytes / 4)`, the estimate used when a harness omits usage."""
    return -(-sum(len(text.encode()) for text in texts) // 4)


def _json_object(text: str | None) -> dict | None:
    try:
        value = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# Cache


def _cache_path(home: Path, name: str) -> Path:
    return home / "probes" / f"{name}.json"


def _save(home: Path, name: str, backend: dict, result: ProbeResult) -> None:
    path = _cache_path(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"version": result.version, "config": backend, "result": asdict(result)}
    path.write_text(json.dumps(entry, indent=2))


def _load(home: Path, name: str, backend: dict, version: str) -> ProbeResult | None:
    path = _cache_path(home, name)
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if entry.get("version") != version or entry.get("config") != backend:
        return None
    return ProbeResult(**entry["result"])


# Version


def _version(backend: dict) -> tuple[str, str]:
    """The harness version, or an empty version and the reason it is unavailable."""
    harness = backend["harness"]
    if harness == "lmstudio":
        return LMSTUDIO_VERSION, ""
    result = run_process([harness, "--version"], timeout=VERSION_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return "", f"{harness} --version failed: {detail}"
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "", ""


# apsis


def _apsis(cfg: dict, args: list[str], cwd: Path) -> dict | None:
    result = run_process(
        [cfg["apsis"]["command"], *args], cwd=cwd, timeout=PROBE_TIMEOUT_SECONDS
    )
    return _json_object(result.stdout) if result.returncode == 0 else None


def _apsis_opens(cfg: dict, source: str, session_id: str, cwd: Path) -> bool:
    args = ["--all-projects", "-s", source, "-f", "shared-json", session_id]
    doc = _apsis(cfg, args, cwd)
    return bool(doc) and doc.get("kind") == "session"


def _apsis_lists(cfg: dict, thread_id: str, extra: list[str], cwd: Path) -> bool:
    doc = _apsis(cfg, ["--all-projects", "--list", "-f", "shared-json", *extra], cwd)
    rows = (doc or {}).get("sessions") or []
    return any(row.get("thread_id") == thread_id for row in rows)


# Probes


def _prepare(home: Path, name: str) -> tuple[Path, str]:
    """The probe's working directory, with canary instruction files at and above it."""
    canary = uuid.uuid4().hex
    base = home / "probes" / name
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    for directory in (base, work):
        for filename in ("CLAUDE.md", "AGENTS.md"):
            (directory / filename).write_text(f"DREAMING-CANARY: {canary}\n")
    return work, canary


def _give_up(attempts: list[ProbeResult]) -> ProbeResult:
    """The last attempt that reached a model (else the last attempt), with every
    attempt's problems as the reason."""
    reached = [a for a in attempts if a.status == "probed"]
    chosen = reached[-1] if reached else attempts[-1]
    reasons = []
    for attempt in attempts:
        problems = (
            attempt.problems() if attempt.status == "probed" else [attempt.reason]
        )
        reasons.append(f"{attempt.isolation}: {'; '.join(problems)}")
    chosen.reason = " | ".join(reasons)
    return chosen


def _probe_claude(
    home: Path, cfg: dict, name: str, backend: dict, version: str
) -> ProbeResult:
    work, canary = _prepare(home, name)
    attempts = []
    for isolation in claude.ISOLATIONS:
        session_id = str(uuid.uuid4())
        argv = claude.command(
            backend,
            isolation=isolation,
            session_id=session_id,
            tools=PROBE_TOOLS,
            mcp_config={"mcpServers": {}},
            schema=PROBE_SCHEMA,
        )
        proc = run_process(
            argv,
            cwd=work,
            env=claude.environment(backend, isolation),
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin_text=PROBE_PROMPT,
        )
        init, final = claude.parse_stream(proc.stdout)
        base = ProbeResult(name, "claude", version, isolation=isolation.name)
        if proc.returncode != 0 or final is None or final.get("is_error"):
            detail = proc.stderr.strip() or (final or {}).get("result") or "no result"
            base.status, base.reason = "unavailable", str(detail)
            attempts.append(base)
            continue

        structured = isinstance(final.get("structured_output"), dict)
        answer = final["structured_output"] if structured else final.get("result")
        tools = list(init.get("tools") or [])
        agent = any(tool in claude.AGENT_TOOLS for tool in tools)
        leaks = [
            leak
            for leak, present in (
                ("instructions", canary in json.dumps(answer)),
                ("skills", bool(init.get("skills"))),
                ("mcp", bool(init.get("mcp_servers"))),
                ("agent_tool", agent),
            )
            if present
        ]
        usage = claude.usage(final)
        base.status = "probed"
        base.tools, base.leaks, base.structured_output = tools, leaks, structured
        base.agent_tool_removable = not agent
        base.agent_enforcement = "" if agent else "removed"
        base.session_ref = session_id
        base.tokens_in, base.tokens_out = usage or (
            _estimate(PROBE_PROMPT),
            _estimate(json.dumps(answer)),
        )
        base.usage_estimated = usage is None
        base.apsis_finds_session = _apsis_opens(cfg, "claude", session_id, work)
        attempts.append(base)
        if base.usable:
            return base
    return _give_up(attempts)


def _isolated_codex_home(home: Path) -> Path:
    """An empty `CODEX_HOME` that keeps the user's login through a link to auth.json."""
    isolated = home / "codex-home"
    isolated.mkdir(exist_ok=True)
    auth, link = codex.user_codex_home() / "auth.json", isolated / "auth.json"
    if auth.exists() and not link.is_symlink():
        link.symlink_to(auth)
    return isolated


def _probe_codex(
    home: Path, cfg: dict, name: str, backend: dict, version: str
) -> ProbeResult:
    work, canary = _prepare(home, name)
    streams = home / "probes" / name / "codex"
    schema_path = home / "probes" / name / "schema.json"
    schema_path.write_text(json.dumps(PROBE_SCHEMA))
    candidates = (
        ("user-home", dict(os.environ)),
        (
            "isolated-home",
            {**os.environ, "CODEX_HOME": str(_isolated_codex_home(home))},
        ),
    )
    attempts = []
    for isolation, env in candidates:
        result = ProbeResult(name, "codex", version, isolation=isolation)
        view = run_process(
            codex.prompt_view_command("probe"),
            cwd=work,
            env=env,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if view.returncode != 0:
            result.status = "unavailable"
            result.reason = view.stderr.strip() or "prompt view failed"
            attempts.append(result)
            continue
        result.leaks = codex.view_leaks(view.stdout, canary)
        if set(result.leaks) & set(BLOCKING_LEAKS):
            result.status = "probed"
            attempts.append(result)
            continue

        proc = run_process(
            codex.exec_command(backend, cwd=work, schema_path=schema_path),
            cwd=work,
            env=env,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin_text=PROBE_PROMPT,
        )
        events = codex.parse_events(proc.stdout)
        link = home / "codex-home" / "auth.json"
        if isolation == "isolated-home" and link.exists() and not link.is_symlink():
            # Codex rewrote its login in the isolated home; the user's own
            # auth.json may now hold a stale token, so never use this set.
            result.status = "unavailable"
            result.reason = (
                f"codex replaced the auth.json link in {link.parent}; "
                f"check that {codex.user_codex_home()} still has a valid login"
            )
            attempts.append(result)
            continue
        if events.thread_id:
            streams.mkdir(parents=True, exist_ok=True)
            (streams / f"{events.thread_id}.jsonl").write_text(proc.stdout)
        if proc.returncode != 0 or events.error or events.message is None:
            result.status = "unavailable"
            result.reason = events.error or proc.stderr.strip() or "no final message"
            attempts.append(result)
            continue

        answer = _json_object(events.message)
        result.status = "probed"
        result.structured_output = answer is not None
        result.tools = [str(tool) for tool in (answer or {}).get("tools", [])]
        if canary in events.message and "instructions" not in result.leaks:
            result.leaks.insert(0, "instructions")
        agent = (
            "agent_tool" in result.leaks
            or any(tool in codex.AGENT_TOOLS for tool in result.tools)
            or bool(events.agent_events)
        )
        if agent and "agent_tool" not in result.leaks:
            result.leaks.append("agent_tool")
        result.agent_tool_removable = not agent
        result.agent_enforcement = "kill_on_agent_event" if agent else "removed"
        result.session_ref = events.thread_id
        usage = codex.usage(events)
        result.tokens_in, result.tokens_out = usage or (
            _estimate(PROBE_PROMPT),
            _estimate(events.message),
        )
        result.usage_estimated = usage is None
        exec_dir = ["--codex-exec-dir", str(streams)]
        result.apsis_finds_session = _apsis_lists(cfg, events.thread_id, exec_dir, work)
        attempts.append(result)
        if result.usable:
            return result
    return _give_up(attempts)


def _probe_lmstudio(
    home: Path, cfg: dict, name: str, backend: dict, version: str
) -> ProbeResult:
    result = ProbeResult(name, "lmstudio", version, isolation="direct")
    base_url = backend["base_url"] or lmstudio.DEFAULT_URL
    try:
        models = lmstudio.list_models(base_url, timeout=10)
    except lmstudio.LMStudioError as exc:
        result.status, result.reason = "unavailable", f"LM Studio at {base_url}: {exc}"
        return result
    model = backend["model"]
    if (model and model not in models) or not models:
        result.status = "unavailable"
        result.reason = (
            f"model {model or '(any)'} is not loaded in LM Studio at {base_url}"
        )
        return result

    thread_id = f"dream-probe-{uuid.uuid4().hex}"
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    prompt = 'Reply with the JSON object {"ok": true}.'
    body = lmstudio.request_body(
        thread_id, model, [{"role": "user", "content": prompt}], schema
    )
    try:
        reply = lmstudio.chat(base_url, body, timeout=PROBE_TIMEOUT_SECONDS)
        content = reply["choices"][0]["message"]["content"]
    except (lmstudio.LMStudioError, KeyError, IndexError, TypeError) as exc:
        result.status, result.reason = "unavailable", f"LM Studio call failed: {exc}"
        return result

    usage = reply.get("usage") or {}
    result.status = "probed"
    result.structured_output = _json_object(content) is not None
    result.agent_tool_removable, result.agent_enforcement = True, "removed"
    result.session_ref = thread_id
    if usage:
        result.tokens_in = int(usage.get("prompt_tokens") or 0)
        result.tokens_out = int(usage.get("completion_tokens") or 0)
    else:
        result.tokens_in, result.tokens_out = _estimate(prompt), _estimate(content)
        result.usage_estimated = True
    log_dir = cfg["apsis"]["lmstudio_log_dir"]
    extra = ["--lmstudio-log-dir", log_dir] if log_dir else []
    result.apsis_finds_session = _apsis_lists(cfg, thread_id, extra, home)
    if not result.usable:
        result.reason = "; ".join(result.problems())
    return result


_PROBES = {"claude": _probe_claude, "codex": _probe_codex, "lmstudio": _probe_lmstudio}


def probe_backend(home: Path, cfg: dict, name: str) -> ProbeResult:
    """Probe one backend with a real call and cache the result."""
    backend = _backend(cfg, name)
    version, problem = _version(backend)
    if problem:
        return ProbeResult(
            name, backend["harness"], status="unavailable", reason=problem
        )
    result = _PROBES[backend["harness"]](home, cfg, name, backend, version)
    result.probed_at = datetime.now(UTC).isoformat(timespec="seconds")
    if result.status == "probed":
        _save(home, name, backend, result)
    return result


def cached_status(home: Path, cfg: dict, name: str) -> ProbeResult:
    """The dry-run view: `--version` and the cache only, with no model call."""
    backend = _backend(cfg, name)
    version, problem = _version(backend)
    if problem:
        return ProbeResult(
            name, backend["harness"], status="unavailable", reason=problem
        )
    cached = _load(home, name, backend, version)
    if cached is not None:
        cached.status = "cached"
        return cached
    return ProbeResult(
        name,
        backend["harness"],
        version,
        status="unprobed",
        reason="not probed at this version and configuration",
    )


def describe(result: ProbeResult) -> str:
    """One line per backend for `dream probe`."""

    def flag(value: bool | None) -> str:
        return "unknown" if value is None else ("yes" if value else "no")

    parts = [
        f"{result.backend}: {result.status}",
        "usable" if result.usable else "not usable",
        result.version or "-",
        f"isolation={result.isolation or '-'}",
        f"leaks={','.join(result.leaks) or 'none'}",
        f"structured={flag(result.structured_output)}",
        f"apsis={flag(result.apsis_finds_session)}",
    ]
    line = "  ".join(parts)
    return f"{line}\n    {result.reason}" if result.reason else line

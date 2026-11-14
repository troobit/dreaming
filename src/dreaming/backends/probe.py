"""Version-keyed, recorded probes of harness isolation and structured output."""

import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from jsonschema import ValidationError, validate

from dreaming import __version__
from dreaming.backends.process import run_process
from dreaming.config import (
    BackendConfig,
    Config,
    ConfigError,
    ensure_home,
    home_path,
    require_external,
)
from dreaming.redact import redact
from dreaming.store import Store

SCHEMA = {
    "type": "object",
    "properties": {
        key: {"type": "array", "items": {"type": "string"}}
        for key in ("tools", "instruction_files", "skills")
    },
    "required": ["tools", "instruction_files", "skills"],
    "additionalProperties": False,
}
PROMPT = (
    "Capability probe only. Do not call any tool or start any agent. Return JSON containing "
    "tools (the exact names of all tools available in this session), instruction_files "
    "(user/project AGENTS.md, CLAUDE.md or other customization files loaded into this session), "
    "and skills (the names of all loaded skills). Use empty arrays when none are loaded."
)
CODEX_FLAGS = (
    "-c",
    "skills.include_instructions=false",
    "-c",
    'user_instructions=""',
    "-c",
    'developer_instructions=""',
    "-c",
    "project_doc_max_bytes=0",
    "-c",
    "features.multi_agent=false",
)


@dataclass(frozen=True)
class ProbeResult:
    backend: str
    harness: str
    version: str = "unknown"
    state: str = "unprobed"
    compatible: bool = False
    reason: str = "not probed"
    tools: tuple[str, ...] = ()
    instruction_leaks: tuple[str, ...] = ()
    skill_leaks: tuple[str, ...] = ()
    agent_tools: tuple[str, ...] = ()
    agent_tool_removable: bool | None = None
    enforcement_required: bool = False
    structured_output: bool | None = None
    apsis_visible: bool | None = None
    session_ref: str | None = None
    flags: tuple[str, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    usage_estimated: bool = False
    call_count: int = 0
    context_tokens: int = 0


def _events(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except ValueError:
            continue
    return result


def _tools(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_tools(v) for v in value)) if value else set()
    if isinstance(value, dict):
        nested = [value[k] for k in ("function", "tools", "functions") if k in value]
        if nested:
            return set().union(*(_tools(item) for item in nested))
        if isinstance(value.get("name"), str):
            return {value["name"]}
    return set()


def _agent_tools(tools: set[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            t
            for t in tools
            if t.lower() == "agent"
            or any(x in t for x in ("spawn_agent", "codex-agent", "gemini-agent"))
        )
    )


def _usage(events: list[dict], prompt: str, output: str) -> tuple[int, int, bool]:
    # Claude's result and Codex's turn.completed carry aggregate usage for this call.
    for event in reversed(events):
        usage = event.get("usage")
        if isinstance(usage, dict) and all(
            type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")
        ):
            return (
                usage["input_tokens"]
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0),
                usage["output_tokens"],
                False,
            )
    return math.ceil(len(prompt.encode()) / 4), math.ceil(len(output.encode()) / 4), True


def _visible(source: str, session_ref: str, exec_dir: Path | None) -> bool:
    from dreaming.sessions import ApsisClient, SessionError

    try:
        client = ApsisClient(codex_exec_dirs=(exec_dir,) if exec_dir else ())
        identifier = session_ref
        if source == "codex-exec":
            matches = [
                row
                for row in client.list().sessions
                if (row.source == "codex-exec" and row.thread_id == session_ref)
                or (row.source == "codex" and row.id == session_ref)
            ]
            if not matches:
                return False
            # apsis suppresses a saved exec stream when the native rollout also exists.
            source, identifier = matches[0].source, matches[0].id
        document = client.open(identifier, source)
        return document.metadata.id == session_ref or document.metadata.thread_id == session_ref
    except SessionError:
        return False


def _cache_path(home: Path, backend: BackendConfig, name: str, version: str) -> Path:
    key = json.dumps(
        {"protocol": 1, "name": name, "version": version, **asdict(backend)}, sort_keys=True
    )
    return require_external(home / "probes" / (hashlib.sha256(key.encode()).hexdigest() + ".json"))


def _payload(events: list[dict], final: Path) -> dict:
    if final.is_file():
        return json.loads(final.read_text())
    for event in reversed(events):
        if isinstance(event.get("structured_output"), dict):
            return event["structured_output"]
        if isinstance(event.get("result"), str):
            return json.loads(event["result"])
        item = event.get("item", {})
        if item.get("type") == "agent_message":
            return json.loads(item["text"])
    raise ValueError("No structured probe response")


def probe_backend(
    name: str,
    backend: BackendConfig,
    home: Path,
    *,
    dry_run: bool = False,
    max_tokens: int = 200_000,
    max_seconds: float | None = None,
    visibility=None,
    run_id: str | None = None,
) -> ProbeResult:
    start = time.monotonic()
    result = ProbeResult(name, backend.harness, context_tokens=backend.context_tokens)
    if backend.harness == "lmstudio":
        return replace(
            result, reason="LM Studio tool capabilities must be probed through a harness"
        )
    try:
        version = subprocess.run(
            [backend.command, "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            close_fds=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return replace(result, state="unavailable", reason=f"Backend unavailable: {exc}")
    result = replace(result, version=version)
    cache = _cache_path(home, backend, name, version)
    if dry_run:
        try:
            data = json.loads(cache.read_text())
            for field in ("tools", "instruction_leaks", "skill_leaks", "agent_tools", "flags"):
                data[field] = tuple(data[field])
            return replace(ProbeResult(**data), state="cached")
        except (OSError, ValueError, TypeError, KeyError):
            return result

    home = ensure_home(home)
    run_id = run_id or f"probe-{uuid.uuid4()}"
    if not all(c.isalnum() or c in "-_" for c in run_id):
        raise ConfigError("Invalid probe run id")
    key = "probe:" + hashlib.sha256(name.encode()).hexdigest()[:16]
    cwd = require_external(home / "runs" / run_id / key.replace(":", "-"))
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env.update(CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    env.pop("CLAUDECODE", None)
    schema = cwd / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    debug = []
    debug_reason = ""
    if backend.harness == "codex":
        try:
            check = run_process(
                [backend.command, "debug", "prompt-input", *CODEX_FLAGS, "probe"],
                cwd,
                env,
                backend.timeout_seconds,
            )
            if check.returncode or check.reason:
                debug_reason = check.reason or "Codex prompt inspection unavailable"
            else:
                debug = json.loads(check.stdout)
                if not isinstance(debug, list):
                    debug_reason = "Unknown Codex prompt inspection format"
                    debug = []
        except (OSError, ValueError) as exc:
            debug_reason = f"Codex prompt inspection unavailable: {exc}"
    debug_text = json.dumps(debug)
    debug_tools = set().union(*(_tools(d.get("tools", [])) for d in debug if isinstance(d, dict)))
    instruction_leaks = tuple(
        term
        for term in ("<INSTRUCTIONS>", "<user_instructions>", "AGENTS.md", "CLAUDE.md")
        if term in debug_text
    )
    skill_leaks = (
        ("skills in prompt",)
        if ("Available skills" in debug_text or "<skills_instructions>" in debug_text)
        else ()
    )
    flags = (
        [
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--tools",
            "Read",
            "--permission-mode",
            "dontAsk",
        ]
        if backend.harness == "claude"
        else [
            "--ignore-user-config",
            "--ignore-rules",
            *CODEX_FLAGS,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
        ]
    )
    session_ref = str(uuid.uuid4()) if backend.harness == "claude" else None
    structured = True
    tokens_in = tokens_out = count = 0
    estimated = False
    reason = debug_reason
    final = cwd / "final.json"
    exec_dir = require_external(home / "codex-exec") if backend.harness == "codex" else None
    with Store(home) as store:
        conn = store.connection
        own_run = conn.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone() is None
        if own_run:
            conn.execute(
                "INSERT INTO runs(id,status,revision,started_at) "
                "VALUES (?,'running',?,datetime('now'))",
                (run_id, __version__),
            )
        conn.execute(
            "INSERT OR IGNORE INTO steps(run_id,key,phase) VALUES (?,?,'Probe')", (run_id, key)
        )
        while count < 3:
            if max_seconds is not None and time.monotonic() - start >= max_seconds:
                reason = "probe wall-clock budget exhausted"
                break
            spent = conn.execute(
                "SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM calls WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if spent + math.ceil(len(PROMPT.encode()) / 4) >= max_tokens:
                reason = "probe budget exhausted"
                break
            count += 1
            call_id = str(uuid.uuid4())
            attempt = (
                conn.execute(
                    "SELECT COUNT(*) FROM calls WHERE run_id=? AND task_key=?", (run_id, key)
                ).fetchone()[0]
                + 1
            )
            conn.execute(
                "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,model,session_ref) "
                "VALUES (?,?,?,?,'probe',?,?,?)",
                (call_id, run_id, key, attempt, name, backend.model, session_ref),
            )
            if backend.harness == "claude":
                command = [
                    backend.command,
                    *flags,
                    "--session-id",
                    session_ref,
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
                if structured:
                    command += ["--json-schema", json.dumps(SCHEMA)]
                if backend.model:
                    command += ["--model", backend.model]
                command += ["-p", PROMPT]
            else:
                command = [backend.command, "exec", *flags, "--json", "-o", str(final)]
                if structured:
                    command += ["--output-schema", str(schema)]
                if backend.provider == "lmstudio":
                    command += ["--oss", "--local-provider", "lmstudio"]
                if backend.model:
                    command += ["--model", backend.model]
                command += [PROMPT]
            call_start = time.monotonic()
            try:
                process = run_process(command, cwd, env, backend.timeout_seconds)
            except OSError as exc:
                reason = str(exc)
                conn.execute(
                    "UPDATE calls SET outcome='failed',error=? WHERE id=?",
                    (redact(reason), call_id),
                )
                break
            events = _events(process.stdout)
            if exec_dir:
                exec_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                (exec_dir / f"{call_id}.jsonl").write_text(redact(process.stdout))
                (cwd / f"{call_id}.jsonl").write_text(redact(process.stdout))
            for event in events:
                if event.get("type") == "thread.started":
                    session_ref = event.get("thread_id")
            used_in, used_out, used_estimate = _usage(events, PROMPT, process.stdout)
            tokens_in += used_in
            tokens_out += used_out
            estimated |= used_estimate
            conn.execute(
                "UPDATE calls SET tokens_in=?,tokens_out=?,usage_estimated=?,seconds=?,"
                "raw_output=?,error=?,session_ref=?,outcome=? WHERE id=?",
                (
                    used_in,
                    used_out,
                    int(used_estimate),
                    time.monotonic() - call_start,
                    redact(process.stdout),
                    redact(process.stderr),
                    session_ref,
                    "failed" if process.returncode or process.reason else "returned",
                    call_id,
                ),
            )
            if process.reason:
                reason = process.reason
                break
            if process.returncode:
                error = process.stderr.lower()
                unsupported = "unknown" in error or "unrecognized" in error or "unexpected" in error
                if unsupported and "--safe-mode" in flags:
                    flags = ["--setting-sources", "", *flags[1:]]
                    session_ref = str(uuid.uuid4())
                    continue
                if unsupported and structured and "schema" in error:
                    structured = False
                    continue
                reason = f"Probe call failed: {redact(process.stderr[:500])}"
                break
            try:
                payload = _payload(events, final)
                validate(payload, SCHEMA)
            except (ValueError, OSError, ValidationError):
                reason = "Invalid probe response: expected the capability JSON schema"
                break
            observed_tools = set(payload["tools"])
            for event in events:
                if event.get("type") == "system" and event.get("subtype") == "init":
                    observed_tools |= _tools(event.get("tools", []))
                    skill_leaks += tuple(event.get("skills", []))
            tools = observed_tools | debug_tools
            instruction_leaks += tuple(payload["instruction_files"])
            skill_leaks += tuple(payload["skills"])
            agents = _agent_tools(tools)
            source = "codex-exec" if backend.harness == "codex" else "claude"
            visible = bool(session_ref and (visibility or _visible)(source, session_ref, exec_dir))
            if instruction_leaks or skill_leaks:
                reason = "User/project instructions or skills leaked into probe context"
            elif not visible:
                reason = "apsis cannot open the probe session"
            elif agents and backend.harness != "codex":
                reason = "Agent-launch tools exposed by harness"
            result = replace(
                result,
                tools=tuple(sorted(tools)),
                agent_tools=agents,
                agent_tool_removable=not bool(agents),
                enforcement_required=bool(agents),
                apsis_visible=visible,
            )
            break
        compatible = not reason
        result = replace(
            result,
            state="probed",
            compatible=compatible,
            reason=reason,
            instruction_leaks=tuple(sorted(set(instruction_leaks))),
            skill_leaks=tuple(sorted(set(skill_leaks))),
            flags=tuple(flags),
            structured_output=structured,
            session_ref=session_ref,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usage_estimated=estimated,
            call_count=count,
        )
        conn.execute(
            "UPDATE steps SET state=?,attempts=?,reason=? WHERE run_id=? AND key=?",
            ("done" if compatible else "failed", count, reason, run_id, key),
        )
        if own_run:
            conn.execute(
                "UPDATE runs SET status=?,ended_at=datetime('now'),budget=? WHERE id=?",
                (
                    "budget_exhausted" if "budget" in reason else "complete",
                    json.dumps(
                        {"tokens_used": tokens_in + tokens_out, "seconds": time.monotonic() - start}
                    ),
                    run_id,
                ),
            )
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = cache.with_suffix(f".{uuid.uuid4()}.tmp")
    temporary.write_text(json.dumps(asdict(result), indent=2))
    temporary.chmod(0o600)
    temporary.replace(cache)
    return result


def probe_config(config: Config, *, dry_run: bool = False, names: list[str] | None = None) -> int:
    selected = names or list(
        dict.fromkeys(backend for choices in config.roles.values() for backend in choices)
    )
    if any(name not in config.backends for name in selected):
        raise ConfigError("Unknown backend requested")
    run_id = f"probe-{uuid.uuid4()}"
    home = home_path()
    if not dry_run:
        with Store(home) as store:
            store.connection.execute(
                "INSERT INTO runs(id,status,revision) VALUES (?,'running',?)", (run_id, __version__)
            )
    start = time.monotonic()
    state = "interrupted"
    results = []
    try:
        for name in selected:
            results.append(
                probe_backend(
                    name,
                    config.backends[name],
                    home,
                    dry_run=dry_run,
                    max_tokens=config.budget.max_tokens,
                    max_seconds=config.budget.max_seconds - (time.monotonic() - start),
                    run_id=run_id,
                )
            )
        state = "budget_exhausted" if any("budget" in r.reason for r in results) else "complete"
    finally:
        if not dry_run:
            with Store(home) as store:
                tokens = store.connection.execute(
                    "SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM calls WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                store.connection.execute(
                    "UPDATE runs SET status=?,ended_at=datetime('now'),budget=? WHERE id=?",
                    (
                        state,
                        json.dumps({"tokens_used": tokens, "seconds": time.monotonic() - start}),
                        run_id,
                    ),
                )
    print(json.dumps({"probes": [asdict(result) for result in results]}, indent=2))
    return 0 if dry_run or state == "budget_exhausted" or all(r.compatible for r in results) else 1

"""Bounded subprocesses with immediate rejection of agent-launch events."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from dreaming.config import agent_tool


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    reason: str = ""


def agent_event(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "assistant":
        message = value.get("message", {})
        blocks = message.get("content", []) if isinstance(message, dict) else []
        return isinstance(blocks, list) and any(agent_event(block) for block in blocks)
    if value.get("type") == "content_block_start":
        return agent_event(value.get("content_block"))
    event_type = value.get("type", "")
    if not isinstance(event_type, str) or not (
        event_type in {"item.started", "item.updated", "item.completed", "tool_use", "tool_call"}
        or event_type.startswith("collab_")
    ):
        return False
    item = value.get("item", value)
    if not isinstance(item, dict):
        return False
    for key in ("type", "name", "tool", "tool_name"):
        text = item.get(key, "")
        if isinstance(text, str) and (agent_tool(text) or text.startswith("collab_")):
            return True
    return False


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS may return EPERM for a group whose leader has just exited.
        if process.poll() is None:
            raise


def run_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    *,
    input_text: str | None = None,
) -> ProcessResult:
    output = bytearray()
    pending = bytearray()
    reason = ""
    with tempfile.TemporaryFile(dir=cwd) as errors, tempfile.TemporaryFile(dir=cwd) as prompt:
        if input_text is not None:
            prompt.write(input_text.encode())
            prompt.seek(0)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=prompt if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        reason = "timeout"
                        break
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            try:
                                if pending and agent_event(json.loads(pending)):
                                    reason = "agent-launch event rejected"
                            except ValueError:
                                pass
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        pending.extend(chunk)
                        if len(output) > 8 * 1024 * 1024:
                            reason = "probe output limit exceeded"
                            break
                        while b"\n" in pending:
                            line, _, pending = pending.partition(b"\n")
                            try:
                                if agent_event(json.loads(line)):
                                    reason = "agent-launch event rejected"
                                    break
                            except ValueError:
                                pass
                    if reason:
                        break
            if reason:
                _kill_group(process)
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
                _kill_group(process)
                process.wait()
        except BaseException:
            _kill_group(process)
            process.wait()
            raise
        finally:
            process.stdout.close()
        errors.seek(0)
        return ProcessResult(
            process.returncode,
            output.decode(errors="replace"),
            errors.read(1024 * 1024).decode(errors="replace"),
            reason,
        )

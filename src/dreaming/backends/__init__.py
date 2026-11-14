"""Model backends: harness adapters, the direct LM Studio adapter and probes."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class CallResult:
    """One answered model call (design, Roles and calls)."""

    output: dict
    tokens_in: int
    tokens_out: int
    usage_estimated: bool = False
    session_ref: str = ""  # harness session id, or LM Studio thread id == run id
    model: str | None = None


class Backend(Protocol):
    name: str
    context_tokens: int

    def call(
        self, role: str, prompt: str, schema: dict, tools: list[str]
    ) -> CallResult: ...


def estimate_tokens(*texts: str) -> int:
    """`ceil(utf8 bytes / 4)`, the estimate used when a harness omits usage and
    for sizing inputs."""
    return -(-sum(len(text.encode()) for text in texts) // 4)


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    stdin_text: str | None = None,
) -> ProcessResult:
    """Run a command in its own process group and kill the group on timeout.

    Descriptors beyond stdio are closed, so a model call never inherits the
    dream lock (design, Lock).
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return ProcessResult(127, "", f"{argv[0]}: {exc.strerror or exc}")
    try:
        stdout, stderr = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        return ProcessResult(-signal.SIGKILL, stdout, stderr, timed_out=True)
    return ProcessResult(proc.returncode, stdout, stderr)

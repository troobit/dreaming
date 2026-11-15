"""Direct LM Studio calls for tool-free roles, over its OpenAI-compatible HTTP API.

The run id is each call's client thread id and leads the request body, so LM
Studio's server log, which truncates long bodies, still names the dream; that
log entry is the call's session record (5.8). Which model is loaded, and with
what context, is read from LM Studio's REST API before every call, because the
user can reload a model between calls; dreaming never loads one itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from dreaming.backends import (
    BackendError,
    CallResult,
    estimate_tokens,
    parse_reply,
)
from dreaming.errors import USAGE_ERROR, DreamingError

DEFAULT_URL = "http://localhost:1234"
MODELS_TIMEOUT_SECONDS = 10
CHAT_MODEL_TYPES = ("llm", "vlm")


class LMStudioError(Exception):
    pass


def request_body(
    thread_id: str, model: str, messages: list[dict], schema: dict
) -> dict:
    """A chat request with `client_metadata.thread_id` first, so LM Studio's log keeps
    it even when it truncates a long body (5.8)."""
    body: dict = {"client_metadata": {"thread_id": thread_id}}
    if model:
        body["model"] = model
    body["messages"] = messages
    body["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "output", "strict": True, "schema": schema},
    }
    body["stream"] = False
    return body


def _request(url: str, data: bytes | None, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise LMStudioError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise LMStudioError(str(getattr(exc, "reason", exc))) from exc


def loaded_model(base_url: str, model: str, timeout: float) -> tuple[str, int]:
    """The loaded chat model's id and context length: `model` when it is loaded,
    or the first loaded one when no model is configured."""
    reply = _request(f"{base_url.rstrip('/')}/api/v0/models", None, timeout)
    loaded = [
        entry
        for entry in reply.get("data", [])
        if entry.get("state") == "loaded" and entry.get("type") in CHAT_MODEL_TYPES
    ]
    chosen = [entry for entry in loaded if not model or entry.get("id") == model]
    if not chosen:
        raise LMStudioError(
            f"model {model or '(any)'} is not loaded in LM Studio at {base_url}"
        )
    entry = chosen[0]
    context = entry.get("loaded_context_length") or entry.get("max_context_length")
    return entry["id"], int(context or 0)


def chat(base_url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    return _request(f"{base_url.rstrip('/')}/v1/chat/completions", data, timeout)


class LMStudioBackend:
    """Calls a model loaded in LM Studio directly; only tool-free roles (5.1)."""

    accepts_session_id = False

    def __init__(
        self,
        cfg: dict,
        name: str,
        *,
        home: Path,
        run_id: str,
        isolation: str = "direct",
    ) -> None:
        self.cfg = cfg
        self.name = name
        self.entry = cfg["backends"][name]
        self.context_tokens = self.entry["context_tokens"]
        self.release_date = self.entry["release_date"]
        self.home = home
        self.run_id = run_id
        self.base_url = self.entry["base_url"] or DEFAULT_URL

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
        workdir: Path | None = None,
    ) -> CallResult:
        if tools:
            raise DreamingError(
                f"{self.name} is a direct LM Studio backend, which cannot run the "
                f"{role} role's tools; use a harness (5.1)",
                USAGE_ERROR,
                "invalid_config",
            )
        try:
            model, context = loaded_model(
                self.base_url, self.entry["model"], MODELS_TIMEOUT_SECONDS
            )
        except LMStudioError as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        self.context_tokens = context
        needed = estimate_tokens(prompt)
        if needed > context:
            raise BackendError(
                f"{self.name}: {model} is loaded with a {context}-token context, "
                f"but this {role} input needs about {needed} tokens"
            )
        body = request_body(
            self.run_id, model, [{"role": "user", "content": prompt}], schema
        )
        timeout = self.cfg["thresholds"]["call_timeout_seconds"]
        try:
            reply = chat(self.base_url, body, timeout)
        except LMStudioError as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        try:
            content = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise BackendError(
                f"{self.name}: a reply without a message: {json.dumps(reply)[:500]}"
            ) from None
        usage = reply.get("usage") or {}
        return CallResult(
            parse_reply(content),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            session_ref=self.run_id,
            model=reply.get("model") or model,
        )

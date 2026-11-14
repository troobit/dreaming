"""Direct LM Studio calls for tool-free roles, over its OpenAI-compatible HTTP API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:1234"


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
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise LMStudioError(str(getattr(exc, "reason", exc))) from exc


def list_models(base_url: str, timeout: float) -> list[str]:
    reply = _request(f"{base_url.rstrip('/')}/v1/models", None, timeout)
    return [entry.get("id", "") for entry in reply.get("data", [])]


def chat(base_url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    return _request(f"{base_url.rstrip('/')}/v1/chat/completions", data, timeout)

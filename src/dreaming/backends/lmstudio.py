"""Tool-free local calls; the LM Studio server log is their session record."""

import http.client
import json
import os
import time
from urllib.parse import urlsplit

from dreaming.backends import estimated_tokens, input_tokens
from dreaming.backends.probe import ProbeResult
from dreaming.config import TOOL_FREE, BackendConfig
from dreaming.steps import CallResult


class HTTPFailure(OSError):
    def __init__(self, status: int, text: str):
        self.status = status
        super().__init__(f"HTTP {status}: {text}")


class LMStudioBackend:
    supports_tools = False

    def __init__(self, name: str, config: BackendConfig, run_id: str):
        self.name, self.config, self.run_id = name, config, run_id
        self.context_tokens = config.context_tokens
        self.model = config.model
        self.structured_output = True
        self.release_date = None

    def new_session(self):
        return self.run_id

    def _request(self, path: str, body: dict | None = None, *, deadline: float | None = None):
        url = urlsplit(self.config.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username:
            raise ValueError("LM Studio base_url must be an http(s) server URL")
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        deadline = deadline or time.monotonic() + self.config.timeout_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("LM Studio call timed out")
        connection = cls(url.hostname, url.port, timeout=remaining)
        # One deadline covers connecting, headers and every response chunk.
        try:
            connection.connect()
            sock = connection.sock
            payload = json.dumps(body).encode() if body is not None else None
            prefix = url.path.rstrip("/")
            prefix = prefix[:-3] if prefix.endswith("/v1") else prefix
            headers = {"Content-Type": "application/json"}
            if token := os.environ.get("LM_API_TOKEN"):
                headers["Authorization"] = f"Bearer {token}"
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            connection.request(
                "POST" if body is not None else "GET",
                prefix + path,
                body=payload,
                headers=headers,
            )
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("LM Studio call timed out")
                sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise OSError("LM Studio response exceeds output limit")
            if not 200 <= response.status < 300:
                raise HTTPFailure(response.status, data.decode(errors="replace"))
            return json.loads(data)
        finally:
            connection.close()

    def _loaded(self, *, deadline=None):
        try:
            document = self._request("/api/v1/models", deadline=deadline)
        except HTTPFailure as exc:
            if exc.status not in {404, 405}:
                raise
            document = self._request("/api/v0/models", deadline=deadline)
        loaded = []
        if not isinstance(document, dict):
            raise ValueError("LM Studio model listing is not an object")
        for model in document.get("models", document.get("data", [])):
            if not isinstance(model, dict):
                raise ValueError("LM Studio model listing contains an invalid record")
            if model.get("type") not in {None, "llm", "vlm"}:
                continue
            key = model.get("key", model.get("id"))
            instances = model.get("loaded_instances", [])
            if not instances and model.get("state") == "loaded":
                instances = [
                    {"id": key, "config": {"context_length": model.get("loaded_context_length")}}
                ]
            for instance in instances:
                if self.config.model and self.config.model not in {key, instance.get("id")}:
                    continue
                context = instance.get("config", {}).get("context_length")
                if type(context) is int and context > 0:
                    loaded.append((instance["id"], context, model.get("release_date")))
        if len(loaded) != 1:
            raise OSError(
                "Choose exactly one already-loaded LM Studio model instance with a context length"
            )
        self.model, context, self.release_date = loaded[0]
        self.context_tokens = min(context, self.config.context_tokens)

    def probe(self):
        try:
            self._loaded()
            return ProbeResult(
                self.name,
                "lmstudio",
                state="probed",
                compatible=True,
                reason="Loaded tool-free model",
                tools=(),
                structured_output=self.structured_output,
                context_tokens=self.context_tokens,
            )
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
            return ProbeResult(self.name, "lmstudio", state="unavailable", reason=str(exc))

    def call(self, role: str, prompt: str, schema: dict, tools: list[str]) -> CallResult:
        if role not in TOOL_FREE or tools:
            raise ValueError("Direct LM Studio supports only tool-free roles")
        start = time.monotonic()
        deadline = start + self.config.timeout_seconds
        try:
            self._loaded(deadline=deadline)
            if input_tokens(prompt, schema, tools) > self.context_tokens - min(
                4096, self.context_tokens // 4
            ):
                return CallResult(
                    "",
                    session_ref=self.run_id,
                    outcome="context_changed",
                    error="Loaded context changed; resize pending role input",
                )
            # Preserve this insertion order: server logs may cut the body after this field.
            body = {
                "client_metadata": {"thread_id": self.run_id},
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "max_tokens": min(4096, self.context_tokens // 4),
            }
            if self.structured_output:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": f"dream_{role}", "strict": True, "schema": schema},
                }
            else:
                body["messages"][0]["content"] += (
                    "\n\nReturn only JSON matching this schema:\n" + json.dumps(schema)
                )
            try:
                response = self._request("/v1/chat/completions", body, deadline=deadline)
            except HTTPFailure as exc:
                if exc.status in {400, 422} and any(
                    term in str(exc).lower()
                    for term in ("response_format", "json_schema", "structured output")
                ):
                    # The dispatcher owns the retry and records this failed attempt.
                    self.structured_output = False
                raise
            choice = response["choices"][0]
            raw = choice["message"]["content"]
            if not isinstance(raw, str):
                raise ValueError("LM Studio completion content is not text")
            usage = response.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            known = all(
                type(usage.get(k)) is int and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens")
            )
            return CallResult(
                raw,
                usage["prompt_tokens"] if known else input_tokens(prompt, schema, tools),
                usage["completion_tokens"] if known else estimated_tokens(raw),
                time.monotonic() - start,
                not known,
                self.run_id,
                "schema_failure" if choice.get("finish_reason") == "length" else "success",
                model=self.model,
            )
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            http.client.HTTPException,
        ) as exc:
            return CallResult(
                "",
                seconds=time.monotonic() - start,
                session_ref=self.run_id,
                outcome="transport_failure",
                error=str(exc),
            )

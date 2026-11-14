import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dreaming.backends.lmstudio import LMStudioBackend
from dreaming.config import BackendConfig


@pytest.fixture
def server():
    received, contexts = [], [4096]
    controls = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, None))
            if controls.get("legacy") and self.path == "/api/v1/models":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if controls.get("legacy"):
                self.wfile.write(
                    json.dumps(
                        {
                            "data": [
                                {
                                    "id": "local",
                                    "type": "llm",
                                    "state": "loaded",
                                    "loaded_context_length": contexts[0],
                                }
                            ]
                        }
                    ).encode()
                )
                return
            self.wfile.write(
                json.dumps(
                    {
                        "models": [
                            {
                                "key": "local",
                                "type": "llm",
                                "loaded_instances": []
                                if controls.get("unloaded")
                                else [
                                    {
                                        "id": "local-instance",
                                        "config": {"context_length": contexts[0]},
                                    }
                                ],
                            }
                        ]
                    }
                ).encode()
            )

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, raw))
            if controls.get("unsupported_schema") and "response_format" in json.loads(raw):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"unsupported response_format json_schema")
                return
            self.send_response(200)
            self.end_headers()
            if controls.get("malformed"):
                self.wfile.write(b"not JSON")
                return
            if controls.get("missing_usage"):
                self.wfile.write(
                    json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()
                )
                return
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": '{"ok":true}'},
                                "finish_reason": "length" if controls.get("truncated") else "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                    }
                ).encode()
            )

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", received, contexts, controls
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


def test_loaded_context_refreshed_and_metadata_survives_truncated_log(server):
    url, received, contexts, _ = server
    b = LMStudioBackend(
        "local", BackendConfig(harness="lmstudio", model="local", base_url=url), "run-123"
    )
    assert b.probe().compatible
    assert b.context_tokens == 4096
    result = b.call("reader", "hello", {"type": "object"}, [])
    assert result.outcome == "success" and result.session_ref == "run-123"
    assert (result.tokens_in, result.tokens_out, result.usage_estimated) == (10, 4, False)
    body = [raw for path, raw in received if raw is not None][-1]
    assert json.loads(body[: body.index(b', "model"')] + b"}") == {
        "client_metadata": {"thread_id": "run-123"}
    }
    payload = json.loads(body)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["model"] == "local-instance"
    contexts[0] = 1024
    assert b.call("reviewer", "hello", {"type": "object"}, []).outcome == "success"
    assert b.context_tokens == 1024
    assert sum(path == "/api/v1/models" for path, _ in received) >= 3


def test_direct_adapter_refuses_tool_using_roles_before_http(server):
    url, received, _, _ = server
    b = LMStudioBackend("local", BackendConfig(harness="lmstudio", base_url=url), "run")
    with pytest.raises(ValueError, match="tool-free"):
        b.call("researcher", "hi", {}, [])
    with pytest.raises(ValueError, match="tool-free"):
        b.call("reader", "hi", {}, ["Read"])
    assert not received


def test_context_shrink_rejects_pending_input_without_completion(server):
    url, received, contexts, _ = server
    b = LMStudioBackend(
        "local", BackendConfig(harness="lmstudio", model="local", base_url=url), "run"
    )
    contexts[0] = 256
    result = b.call("reader", "x" * 10000, {}, [])
    assert result.outcome == "context_changed"
    assert all(raw is None for _, raw in received)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("malformed", "transport_failure"),
        ("truncated", "schema_failure"),
        ("missing_usage", "success"),
    ],
)
def test_completion_errors_and_missing_usage(server, mode, expected):
    url, _, _, controls = server
    controls[mode] = True
    backend = LMStudioBackend(
        "local", BackendConfig(harness="lmstudio", model="local", base_url=url), "run"
    )
    result = backend.call("reader", "hello", {"type": "object"}, [])
    assert result.outcome == expected
    if mode == "missing_usage":
        assert result.usage_estimated and result.tokens_in > 0 and result.tokens_out > 0


def test_unsupported_schema_option_leaves_retry_to_dispatcher(server):
    url, received, _, controls = server
    controls["unsupported_schema"] = True
    backend = LMStudioBackend(
        "local", BackendConfig(harness="lmstudio", model="local", base_url=url), "run"
    )
    assert backend.call("reader", "hello", {"type": "object"}, []).outcome == "transport_failure"
    assert not backend.structured_output
    assert backend.call("reader", "hello", {"type": "object"}, []).outcome == "success"
    bodies = [json.loads(raw) for _, raw in received if raw is not None]
    assert len(bodies) == 2 and "response_format" not in bodies[-1]
    assert "schema" in bodies[-1]["messages"][0]["content"]


def test_public_probe_supports_direct_model_and_dry_probe_is_read_only(server, tmp_path):
    from dreaming.backends.probe import probe_backend

    url, received, _, controls = server
    controls["legacy"] = True
    config = BackendConfig(harness="lmstudio", model="local", base_url=url)
    assert not probe_backend("local", config, tmp_path / "private", dry_run=True).compatible
    assert not received
    assert probe_backend("local", config, tmp_path / "private").compatible
    assert not (tmp_path / "private").exists()
    assert [path for path, _ in received] == ["/api/v1/models", "/api/v0/models"]


def test_unloaded_model_is_not_loaded_implicitly(server):
    url, received, _, controls = server
    controls["unloaded"] = True
    backend = LMStudioBackend(
        "local", BackendConfig(harness="lmstudio", model="local", base_url=url), "run"
    )
    assert not backend.probe().compatible
    assert backend.call("reader", "hello", {}, []).outcome == "transport_failure"
    assert all(path == "/api/v1/models" for path, _ in received)

"""A fake LM Studio server: the REST model list and OpenAI-compatible chat."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def model(model_id, *, state="loaded", context=32_768, kind="llm"):
    """A `/api/v0/models` entry; only a loaded model reports its loaded context."""
    entry = {
        "id": model_id,
        "object": "model",
        "type": kind,
        "state": state,
        "max_context_length": 262_144,
    }
    if state == "loaded":
        entry["loaded_context_length"] = context
    return entry


class FakeLMStudio:
    """Serves on a free local port. Chat replies are taken from `replies` in order,
    then `answer`; a reply is a dict sent as JSON or `(status, text)` for an
    error. Every raw chat body is kept, and each thread id is appended to
    `thread_log` for the fake apsis to list."""

    def __init__(self, thread_log=None):
        self.models = [model("qwen3")]
        self.answer = {"ok": True}
        self.usage = {"prompt_tokens": 12, "completion_tokens": 4}
        self.replies = []
        self.bodies = []
        self.delay = 0.0
        self.thread_log = thread_log
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        ).start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def chats(self):
        return [json.loads(body) for body in self.bodies]

    def _reply(self):
        if self.replies:
            return self.replies.pop(0)
        content = json.dumps(self.answer)
        reply = {"model": self.models[0]["id"], "choices": [{"message": {}}]}
        reply["choices"][0]["message"]["content"] = content
        if self.usage is not None:
            reply["usage"] = self.usage
        return reply

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, data):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/v0/models":
                    payload = {"object": "list", "data": fake.models}
                else:
                    payload = {"data": [{"id": m["id"]} for m in fake.models]}
                self._send(200, json.dumps(payload).encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.bodies.append(body)
                if fake.thread_log:
                    thread = json.loads(body)["client_metadata"]["thread_id"]
                    with open(fake.thread_log, "a") as log:
                        log.write(thread + "\n")
                time.sleep(fake.delay)
                reply = fake._reply()
                if isinstance(reply, tuple):
                    status, text = reply
                    self._send(status, text.encode())
                else:
                    self._send(200, json.dumps(reply).encode())

        return Handler

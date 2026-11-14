"""A stdio MCP boundary exposing only a role's explicitly selected tools."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def serve(path: Path) -> int:
    config = json.loads(path.read_text())
    allowed = set(config["allowed_tools"])
    server = config["server"]
    process = subprocess.Popen(
        [server["command"], *server.get("args", [])],
        env={**os.environ, **server.get("env", {})},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        close_fds=True,
    )
    listed, output_lock, input_lock = set(), threading.Lock(), threading.Lock()

    def write(stream, lock, value):
        with lock:
            stream.write(json.dumps(value) + "\n")
            stream.flush()

    def reject(message, stream=sys.stdout, lock=output_lock):
        write(
            stream,
            lock,
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": "Not available to this dream role"},
            },
        )

    def responses():
        for line in process.stdout:
            message = json.loads(line)
            if "method" in message and "id" in message:
                # Servers may not ask the client to start extra model calls or prompts.
                reject(message, process.stdin, input_lock)
                continue
            if message.get("id") in listed:
                listed.discard(message["id"])
                result = message.get("result", {})
                if "tools" in result:
                    result["tools"] = [
                        tool for tool in result["tools"] if tool.get("name") in allowed
                    ]
            write(sys.stdout, output_lock, message)

    reader = threading.Thread(target=responses, daemon=True)
    reader.start()
    try:
        for line in sys.stdin:
            message = json.loads(line)
            method = message.get("method", "")
            if method == "tools/call" and message.get("params", {}).get("name") not in allowed:
                reject(message)
                continue
            if method not in {
                "initialize",
                "ping",
                "tools/list",
                "tools/call",
            } and not method.startswith("notifications/"):
                reject(message)
                continue
            if method == "tools/list":
                listed.add(message["id"])
            write(process.stdin, input_lock, message)
        process.stdin.close()
        process.wait(timeout=5)
        reader.join(timeout=5)
        return process.returncode
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(serve(Path(sys.argv[1])))

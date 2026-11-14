import json
import subprocess
import sys


def test_mcp_filter_hides_tools_and_rejects_unlisted_calls(tmp_path):
    server = tmp_path / "server.py"
    calls = tmp_path / "calls"
    server.write_text("""import json, sys
from pathlib import Path
for line in sys.stdin:
    message=json.loads(line)
    if message['method']=='tools/list':
        result={'tools':[{'name':'web_search'},{'name':'codex-agent'},{'name':'unexpected'}]}
    else:
        Path(sys.argv[1]).write_text(message.get('params',{}).get('name',''))
        result={'content':[]}
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}), flush=True)
""")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "server": {"command": sys.executable, "args": [str(server), str(calls)]},
                "allowed_tools": ["web_search"],
            }
        )
    )
    messages = [
        dict(jsonrpc="2.0", id=1, method="tools/list"),
        dict(jsonrpc="2.0", id=2, method="tools/call", params={"name": "codex-agent"}),
        dict(jsonrpc="2.0", id=3, method="tools/call", params={"name": "web_search"}),
        dict(jsonrpc="2.0", id=4, method="prompts/get", params={"name": "unwanted"}),
    ]
    result = subprocess.run(
        [sys.executable, "-m", "dreaming.backends.mcp_filter", str(config)],
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    responses = {message["id"]: message for message in map(json.loads, result.stdout.splitlines())}
    assert responses[1]["result"]["tools"] == [{"name": "web_search"}]
    assert "error" in responses[2] and "error" in responses[4]
    assert calls.read_text() == "web_search"

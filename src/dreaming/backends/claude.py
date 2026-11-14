"""Claude calls using the isolation flags confirmed by the installed-version probe."""

import json
from urllib.parse import urlsplit, urlunsplit

from dreaming.backends.harness import BUILTINS, HarnessBackend


class ClaudeBackend(HarnessBackend):
    harness = "claude"

    def command(self, role, tools, mcp_tools, definitions, mcp_path, schema_path, final, env):
        flags = list(self.capabilities.flags)
        for option in ("--mcp-config", "--tools", "--permission-mode"):
            while option in flags:
                index = flags.index(option)
                del flags[index : index + 2]
        builtin = [tool for tool in tools if tool in BUILTINS]
        command = [
            self.config.command,
            *flags,
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_path),
            "--tools",
            ",".join(builtin),
            "--allowedTools",
            ",".join([*builtin, *mcp_tools]),
            "--permission-mode",
            "dontAsk",
            "--session-id",
            self.session_ref,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.capabilities.structured_output:
            command += ["--json-schema", json.dumps(json.loads(schema_path.read_text()))]
        if self.config.model:
            command += ["--model", self.config.model]
        if self.config.provider == "lmstudio":
            url = urlsplit(self.config.base_url)
            env["ANTHROPIC_BASE_URL"] = urlunsplit((url.scheme, url.netloc, "", "", ""))
            env["ANTHROPIC_AUTH_TOKEN"] = "lm-studio"
        return [*command, "-p"]

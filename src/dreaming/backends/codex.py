"""Codex JSON streams retained for apsis, with process-level agent-event enforcement."""

import json

from dreaming.backends.harness import HarnessBackend


class CodexBackend(HarnessBackend):
    harness = "codex"

    def command(self, role, tools, mcp_tools, definitions, mcp_path, schema_path, final, env):
        flags = list(self.capabilities.flags)
        if "--sandbox" in flags:
            index = flags.index("--sandbox")
            del flags[index : index + 2]
        command = [
            self.config.command,
            "exec",
            *flags,
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "--json",
            "-o",
            str(final),
        ]
        for server, definition in definitions.items():
            prefix = f"mcp_servers.{server}"
            for key, value in definition.items():
                if key == "env":
                    # TOML inline tables use '=' rather than JSON's ':'.
                    value = (
                        "{"
                        + ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in value.items())
                        + "}"
                    )
                else:
                    value = json.dumps(value)
                command += ["-c", f"{prefix}.{key}={value}"]
            allowed = [t.split("__", 2)[2] for t in mcp_tools if t.startswith(f"mcp__{server}__")]
            command += ["-c", f"{prefix}.enabled_tools={json.dumps(allowed)}"]
        if self.capabilities.structured_output:
            command += ["--output-schema", str(schema_path)]
        if self.config.provider == "lmstudio":
            command += ["--oss", "--local-provider", "lmstudio"]
            env["LM_STUDIO_BASE_URL"] = self.config.base_url
        if self.config.model:
            command += ["--model", self.config.model]
        return [*command, "-"]

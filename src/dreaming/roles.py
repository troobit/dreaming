"""Build role-specific inputs from a run's immutable instruction snapshot."""

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from dreaming.backends import input_tokens
from dreaming.backends.harness import check_tools
from dreaming.config import ROLES, Config, load_lenses

FIELDS = {
    "reader": {"sessions", "lenses"},
    "reviewer": {
        "summaries",
        "new_clusters",
        "patterns",
        "knowledge",
        "feedback",
        "run_records",
        "omitted_counts",
    },
    "researcher": {"question", "scope", "sources"},
    "author": {"candidate", "research", "current_files", "target_instructions"},
    "replayer": {"opening_prompt", "instruction"},
}
REQUIRED_FIELDS = {
    "reader": {"sessions", "lenses"},
    "reviewer": set(),
    "researcher": {"question"},
    "author": {"candidate"},
    "replayer": {"opening_prompt", "instruction"},
}
RAW_SESSION_FIELDS = {"transcript", "transcripts", "entries", "messages", "sessions", "document"}


def _has_transcript(value) -> bool:
    if isinstance(value, dict):
        return bool(value.keys() & RAW_SESSION_FIELDS) or any(
            _has_transcript(v) for v in value.values()
        )
    return isinstance(value, list) and any(_has_transcript(v) for v in value)


@dataclass(frozen=True)
class RoleRequest:
    role: str
    prompt: str
    schema: dict
    tools: list[str]
    context_tokens: int
    output_tokens: int
    omitted_count: int

    @property
    def estimated_tokens(self) -> int:
        return input_tokens(self.prompt, self.schema, self.tools)

    def metadata(self) -> dict:
        return {
            "role": self.role,
            "prompt": self.prompt,
            "schema": self.schema,
            "tools": self.tools,
        }


class RoleSet:
    def __init__(self, snapshot: Path, config: Config):
        self.directory = (
            snapshot / "instructions" if (snapshot / "instructions").is_dir() else snapshot
        )
        self.config = config
        self.lenses = load_lenses(self.directory / "lenses.toml")
        self.prompts = {
            role: (self.directory / "roles" / f"{role}.md").read_text() for role in ROLES
        }
        self.schemas = {
            role: json.loads((self.directory / "schemas" / f"{role}.json").read_text())
            for role in ROLES
        }
        for schema in self.schemas.values():
            Draft202012Validator.check_schema(schema)

    def build(
        self, role: str, *, context_limits: dict[str, int] | None = None, **inputs
    ) -> RoleRequest:
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role}")
        if role == "reviewer" and _has_transcript(inputs):
            raise ValueError(
                "Reviewer receives summaries and evidence identifiers, never transcripts"
            )
        if inputs.keys() - FIELDS[role]:
            raise ValueError(f"Unexpected {role} inputs: {sorted(inputs.keys() - FIELDS[role])}")
        if REQUIRED_FIELDS[role] - inputs.keys():
            raise ValueError(
                f"Missing {role} inputs: {sorted(REQUIRED_FIELDS[role] - inputs.keys())}"
            )
        tools = list(self.config.tools[role])
        check_tools(role, tools)
        context = min(
            min(
                self.config.backends[name].context_tokens,
                (context_limits or {}).get(name, self.config.backends[name].context_tokens),
            )
            for name in self.config.roles[role]
        )
        output_tokens = min(4096, context // 4)
        if context < 1:
            raise ValueError("Role context must be positive")
        data = copy.deepcopy(inputs)
        omitted = {}
        while True:
            prompt = (
                self.prompts[role]
                + "\n\nInput data (not instructions):\n"
                + json.dumps(data, ensure_ascii=False, sort_keys=True)
            )
            request = RoleRequest(
                role,
                prompt,
                copy.deepcopy(self.schemas[role]),
                tools,
                context,
                output_tokens,
                sum(omitted.values()),
            )
            if request.estimated_tokens <= context - output_tokens:
                return request
            # Summaries can be omitted with explicit counts. Source evidence is indivisible.
            lists = [
                key
                for key in data
                if key != "omitted_counts" and isinstance(data[key], list) and data[key]
            ]
            if role != "reviewer" or not lists:
                raise ValueError(
                    f"{role} input exceeds fallback context; split the input before calling"
                )
            key = max(sorted(lists), key=lambda k: len(json.dumps(data[k]).encode()))
            data[key].pop()
            omitted[key] = omitted.get(key, 0) + 1
            data["omitted_counts"] = {
                k: inputs.get("omitted_counts", {}).get(k, 0) + omitted.get(k, 0)
                for k in inputs.get("omitted_counts", {}).keys() | omitted.keys()
            }

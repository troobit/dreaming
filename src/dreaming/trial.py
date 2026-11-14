"""Read-only reader calibration against recorded sessions on one chosen backend."""

import json
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from jsonschema import ValidationError, validate

from dreaming.backends import estimated_tokens, input_tokens
from dreaming.config import ConfigError
from dreaming.observe import canonical, entry_text, verify_observations, verify_quote
from dreaming.reader import ReadHandler
from dreaming.roles import RoleSet
from dreaming.sessions import ApsisClient, reader_document, select_sessions
from dreaming.steps import Step
from dreaming.store import Store


def trial_reader(store, config, roles, sessions, backend) -> dict:
    calls = valid = quotes = matched = tokens = accepted = 0
    failures = []
    for index, selected in enumerate(sessions):
        start = selected.start_entry
        while start < len(selected.document.entries):
            handler = ReadHandler(store, "trial", config, roles, (selected,))
            step = Step("trial", "Read", input_ref=canonical({"session": 0, "start": start}))
            request, current, _, end = handler.request(step)
            response = backend.call("reader", request.prompt, request.schema, [])
            calls += 1
            tokens += response.tokens_in + response.tokens_out or (
                input_tokens(request.prompt, request.schema, [])
                + estimated_tokens(response.raw_output)
            )
            try:
                if response.outcome != "success":
                    raise ValueError(response.outcome)
                data = json.loads(response.raw_output)
                validate(data, request.schema)
            except (ValueError, ValidationError):
                failures.append(
                    {"session": index, "start_entry": start, "reason": "invalid_output"}
                )
            else:
                valid += 1
                entries = {
                    f"entry:{e.source_index}": e
                    for e in reader_document(store, current, config).entries
                }
                for observation in data["observations"]:
                    quotes += 1
                    entry = entries.get(observation["source_location"])
                    matched += int(
                        entry is not None and verify_quote(entry_text(entry), observation["quote"])
                    )
                gate = verify_observations(
                    store,
                    config,
                    current,
                    data["observations"],
                    backend=backend.name,
                    model=response.model,
                )
                accepted += len(gate["accepted"])
            start = end
    return dict(
        sessions=len(sessions),
        calls=calls,
        schema_valid_rate=valid / calls if calls else 0,
        quote_match_rate=matched / quotes if quotes else 0,
        quotes=quotes,
        accepted_observations=accepted,
        tokens=tokens,
        failures=failures,
    )


def run_trial(config, home, *, backend_name, max_sessions, client=None) -> dict:
    if backend_name not in config.backends or type(max_sessions) is not int or max_sessions <= 0:
        raise ConfigError("Trial requires a configured backend and a positive session count")
    if not config.instruction_repo:
        raise ConfigError("Trial requires an instruction repository")
    from dreaming.config import git, instruction_revision, require_external

    home = require_external(home)
    _, revision = instruction_revision(config)
    chosen = config.backends[backend_name]
    config = replace(config, roles={**config.roles, "reader": (backend_name,)})
    # CLI harness probes and instruction extraction use a throwaway home. No
    # writable Store or cache is opened under the user's DREAMING_HOME.
    with tempfile.TemporaryDirectory(prefix="dreaming-trial-") as directory:
        temporary = Path(directory)
        instructions = temporary / "instructions"
        paths = git(
            config.instruction_repo, "ls-tree", "-r", "--name-only", revision, "instructions/"
        ).splitlines()
        for path in paths:
            target = temporary / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(git(config.instruction_repo, "show", f"{revision}:{path}"))
        roles = RoleSet(instructions, config)
        trial_id = "trial-" + uuid.uuid4().hex
        if chosen.harness == "lmstudio":
            from dreaming.backends.lmstudio import LMStudioBackend

            backend = LMStudioBackend(backend_name, chosen, trial_id)
        else:
            from dreaming.backends.claude import ClaudeBackend
            from dreaming.backends.codex import CodexBackend

            cls = ClaudeBackend if chosen.harness == "claude" else CodexBackend
            backend = cls(backend_name, chosen, temporary / "home", trial_id)
        probe = backend.probe()
        if not probe.compatible:
            raise ConfigError(f"Trial backend unavailable: {probe.reason}")
        config = replace(
            config,
            backends={
                **config.backends,
                backend_name: replace(
                    chosen,
                    context_tokens=min(chosen.context_tokens, backend.context_tokens),
                ),
            },
        )
        roles.config = config
        with Store.readonly(home) as store:
            selection = select_sessions(
                client or ApsisClient(codex_exec_dirs=(home / "codex-exec",)),
                store,
                config,
                run_id=trial_id,
                lenses=roles.lenses,
                reread=True,
                max_sessions=max_sessions,
            )
            return trial_reader(store, config, roles, selection.sessions, backend)

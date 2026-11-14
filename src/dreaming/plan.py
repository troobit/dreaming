"""Read-only planning and a derived rune view; neither controls execution."""

import hashlib
import html
import json
import os
import re
import tempfile
import tomllib
from dataclasses import asdict
from pathlib import Path

from dreaming.backends.probe import probe_backend
from dreaming.config import (
    Config,
    ConfigError,
    git,
    instruction_revision,
    parse_lenses,
    require_external,
)
from dreaming.sessions import ApsisClient, SessionScope, select_sessions
from dreaming.steps import Steps
from dreaming.store import Store


def dry_run(
    config: Config,
    home: Path,
    *,
    client=None,
    now=None,
    scope=None,
    since=None,
    until=None,
    reread=False,
) -> dict:
    home = require_external(home)
    lenses = ()
    if config.instruction_repo:
        _, revision = instruction_revision(config)
        try:
            lenses = parse_lenses(
                tomllib.loads(
                    git(config.instruction_repo, "show", f"{revision}:instructions/lenses.toml")
                )
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid committed lenses: {exc}") from exc
    client = client or ApsisClient(codex_exec_dirs=(home / "codex-exec",))
    with Store.readonly(home) as store:
        selection = select_sessions(
            client,
            store,
            config,
            run_id="dry-run",
            lenses=lenses,
            scope=scope or SessionScope(),
            since=since,
            until=until,
            reread=reread,
            now=now,
        )
    names = dict.fromkeys(name for choices in config.roles.values() for name in choices)
    backends = {
        name: asdict(probe_backend(name, config.backends[name], home, dry_run=True))
        for name in names
    }
    roles = {}
    for role, choices in config.roles.items():
        compatible = next((name for name in choices if backends[name]["compatible"]), None)
        roles[role] = {
            "choices": list(choices),
            "selected": compatible,
            "state": "cached"
            if compatible
            else "unavailable"
            if all(backends[n]["state"] in {"cached", "unavailable"} for n in choices)
            else "unprobed",
        }
    return {
        "dry_run": True,
        "sessions": [
            {
                "id": s.document.metadata.id,
                "source": s.document.metadata.source,
                "project": s.document.metadata.project,
                "start_entry": s.start_entry,
                "lenses": [lens.id for lens in s.lenses],
            }
            for s in selection.sessions
        ],
        "skipped": [asdict(s) for s in selection.skipped],
        "diagnostics": selection.diagnostics,
        "roles": roles,
        "backends": backends,
        "budget": asdict(config.budget),
    }


def render_plan(store: Store, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ConfigError("Invalid run identifier")
    if not store.connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
        raise ConfigError(f"Unknown run: {run_id}")
    rows = Steps(store, run_id).list()
    identifiers = {
        row["key"]: hashlib.sha256(f"{run_id}:{i}".encode()).hexdigest()[:7]
        for i, row in enumerate(rows)
    }
    lines = ["# Dream plan", ""]
    for i, row in enumerate(rows, 1):
        state = {"done": "x", "running": "-"}.get(row["state"], " ")
        title = html.escape(f"{row['phase']}: {row['key']}").replace("\t", " ")
        lines.append(f"- [{state}] {i}. {title} <!-- id:{identifiers[row['key']]} -->")
        lines.append(f"  - State: {row['state']}")
        dependencies = json.loads(row["depends_on"])
        if dependencies:
            lines.append("  - Blocked-by: " + ", ".join(identifiers[key] for key in dependencies))
        lines.append("")
    parent = require_external(store.path.parent / "runs" / run_id)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = require_external(parent / "tasks-dream.md")
    fd, temporary = tempfile.mkstemp(prefix=".plan-", dir=parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path

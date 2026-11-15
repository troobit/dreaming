"""The private home (6.4), configuration, lenses and the run snapshot (7.1, 7.3).

Configuration is `~/.dreaming/config.toml` merged over `DEFAULTS`. The result is
a plain JSON-serialisable dict, so the snapshot of a run is the dict itself.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from dreaming.errors import USAGE_ERROR, DreamingError

ROLES = ("reader", "reviewer", "researcher", "author", "replayer")
HARNESSES = ("claude", "codex", "lmstudio")
AGENT_LAUNCH_TOOLS = ("codex-agent", "gemini-agent")
# Roles that get no tools at all (4.4); the replayer also has no network.
TOOL_FREE_ROLES = ("reader", "reviewer", "replayer")
# File tools every harness provides; any other tool is `<server>.<tool>` from the
# shared MCP definitions (4.4).
BUILTIN_TOOLS = ("read", "grep", "glob", "edit", "write")
PROPOSALS = ("knowledge", "learning", "change")
FRACTIONS = {
    "review_author_reserve",
    "new_cluster_share",
    "max_revision_fraction",
    "replay_max_recurrence",
    "context_weight_share",
}

DEFAULTS: dict[str, Any] = {
    "budget": {
        "sessions": 50,
        "tokens": 2_000_000,
        "wall_clock_minutes": 240,
        "review_author_reserve": 0.3,
    },
    "thresholds": {
        "min_sessions": 3,
        "recent_window_minutes": 60,
        "max_usage_wait_minutes": 300,
        "fallback_on_usage_limit": False,
        "call_timeout_seconds": 1800,
        "breaker_k": 3,
        "new_cluster_share": 0.5,
        "boost_after_dreams": 3,
        "max_revision_fraction": 0.2,
        "revalidate_after_days": 90,
        "recurrence_sessions": 10,
        "max_changes_per_pattern": 3,
        "min_sessions_for_rate": 5,
        "quote_chars": 200,
        "replay_max_sessions": 5,
        "replay_min_sessions": 2,
        "replay_max_recurrence": 0.5,
        "currency_min_sources": 2,
        "max_sources": 20,
        "context_weight_share": 0.25,
    },
    "ranking": {
        "w_sessions": 1.0,
        "w_recency": 1.0,
        "w_projects": 0.5,
        "half_life_days": 30.0,
    },
    # Generate, lint, test and deploy commands are killed after the timeout.
    "adoption": {"auto_apply": False, "command_timeout_seconds": 1800},
    "report": {"open_browser": True},
    "scan": {"names_file": "", "quote_chars": 40},
    "instructions": {"repo": "", "branch": "main"},
    "apsis": {"command": "apsis", "codex_exec_dirs": [], "lmstudio_log_dir": ""},
    "knowledge_folders": [],
    # Skills, rules and workflows a currency review checks, each `<repo>:<path>`
    # in a configured repository (16.1).
    "currency": {"targets": []},
    # The shared MCP definitions (agentic-coding's mcp/servers.json); a call gets
    # only the servers and tools its role lists.
    "mcp": {"servers_file": ""},
    "tools": {
        "reader": [],
        "reviewer": [],
        "researcher": [
            "read",
            "grep",
            "glob",
            "devtools.internet_search",
            "devtools.fetch_url",
        ],
        "author": ["read", "grep", "glob", "edit", "write"],
        "replayer": [],
    },
    "backends": {
        "claude": {"harness": "claude", "context_tokens": 200_000},
        "codex": {"harness": "codex", "context_tokens": 200_000},
    },
    "roles": {role: {"backends": ["claude"]} for role in ROLES},
    "classification": {
        "rules": [
            {"provider": "lmstudio", "locality": "local"},
            {"provider": "ollama", "locality": "local"},
            {"provider": "anthropic", "locality": "cloud"},
            {"provider": "openai", "locality": "cloud"},
            {"model": "claude-*", "locality": "cloud"},
        ]
    },
    "origins": {
        "non_interactive_sources": ["codex-exec", "lmstudio"],
        "claude": {
            "interactive": ["cli", "claude-vscode", "claude-desktop"],
            "non_interactive": ["sdk-cli", "sdk-ts", "sdk-py"],
        },
        "codex": {
            "interactive": ["codex_cli_rs", "codex_vscode"],
            "non_interactive": ["codex_exec", "codex_sdk_ts"],
        },
    },
    "domains": {"projects": {}, "sources": {}},
    "repositories": {},
}

# Sections whose keys the user names; validated by their own functions below.
OPEN_SECTIONS = (
    "backends",
    "roles",
    "classification",
    "origins",
    "domains",
    "repositories",
)

_BACKEND_KEYS = {
    "harness": "",
    "model": "",
    "provider": "",
    "context_tokens": 0,
    "base_url": "",
    "release_date": "",
}
_REPOSITORY_KEYS = {
    "path": "",
    "default_branch": "main",
    "public": False,
    "generate": [],
    "lint": [],
    "test": [],
    "deploy": [],
}


# The private home


def dreaming_home() -> Path:
    """`DREAMING_HOME`, else `~/.dreaming`."""
    raw = os.environ.get("DREAMING_HOME")
    if raw:
        return Path(raw).expanduser().absolute()
    return Path(os.environ.get("HOME") or Path.home()) / ".dreaming"


def containing_repository(path: Path) -> Path | None:
    """The nearest directory at or above `path` that holds a `.git` entry, if any.

    A `.git` file counts too, so linked worktrees and submodules are found.
    """
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ensure_home() -> Path:
    """Create the home with mode 0700, refusing any location inside a git repository."""
    home = dreaming_home()
    repo = containing_repository(home)
    if repo is not None:
        raise DreamingError(
            f"DREAMING_HOME {home} is inside the git repository {repo}; "
            "choose a location outside every repository",
            USAGE_ERROR,
            "home_in_repository",
        )
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if home.stat().st_uid != os.getuid():
        raise DreamingError(
            f"DREAMING_HOME {home} is not owned by this user", USAGE_ERROR
        )
    home.chmod(0o700)
    return home


# Loading and validation


def _invalid(path: str, problem: str, code: str = "invalid_config") -> DreamingError:
    return DreamingError(f"config {path}: {problem}", USAGE_ERROR, code)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _scalar(default: Any, value: Any, path: str) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        raise _invalid(path, "must be true or false")
    if isinstance(default, int | float):
        number = isinstance(value, int | float) and not isinstance(value, bool)
        if isinstance(default, int) and not isinstance(value, int):
            number = False
        if not number or value < 0:
            raise _invalid(path, "must be a non-negative number")
        if path.rsplit(".", 1)[-1] in FRACTIONS and value > 1:
            raise _invalid(path, "must be between 0 and 1")
        return float(value) if isinstance(default, float) else value
    if isinstance(default, str):
        if isinstance(value, str):
            return value
        raise _invalid(path, "must be a string")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise _invalid(path, "must be a list of strings")


def _merge(default: Any, value: Any, path: str) -> Any:
    """Merge `value` over `default`, rejecting unknown keys and wrong types."""
    if not isinstance(default, dict):
        return _scalar(default, value, path)
    if not isinstance(value, dict):
        raise _invalid(path, "must be a table")
    merged = copy.deepcopy(default)
    for key, item in value.items():
        if key not in default:
            raise _invalid(_join(path, key), "unknown setting")
        merged[key] = _merge(default[key], item, _join(path, key))
    return merged


def _table(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise _invalid(path, "must be a table")
    return value


def _backends(value: Any) -> dict:
    backends = copy.deepcopy(DEFAULTS["backends"])
    for name, entry in _table(value, "backends").items():
        backends[name] = _table(entry, f"backends.{name}")
    result = {}
    for name, entry in backends.items():
        path = f"backends.{name}"
        backend = _merge(_BACKEND_KEYS, entry, path)
        if backend["harness"] not in HARNESSES:
            raise _invalid(f"{path}.harness", f"must be one of {', '.join(HARNESSES)}")
        if backend["context_tokens"] <= 0:
            raise _invalid(f"{path}.context_tokens", "must be a positive integer")
        result[name] = backend
    return result


def _roles(value: Any, backends: dict) -> dict:
    roles = copy.deepcopy(DEFAULTS["roles"])
    for role, entry in _table(value, "roles").items():
        if role not in ROLES:
            raise _invalid(
                f"roles.{role}", f"unknown role; roles are {', '.join(ROLES)}"
            )
        entry = _merge({"backends": []}, entry, f"roles.{role}")
        roles[role] = {
            "backends": _role_backend_list(entry["backends"], backends, role)
        }
    return roles


def _role_backend_list(names: Any, backends: dict, role: str) -> list[str]:
    path = f"roles.{role}.backends"
    if not isinstance(names, list) or not names:
        raise _invalid(path, "must be a non-empty list of backend names")
    for name in names:
        if name not in backends:
            raise _invalid(path, f"names undefined backend {name!r}")
    return list(names)


def _classification(value: Any) -> dict:
    user_rules = _table(value, "classification").get("rules", [])
    extra = set(value) - {"rules"}
    if extra:
        raise _invalid(f"classification.{sorted(extra)[0]}", "unknown setting")
    if not isinstance(user_rules, list):
        raise _invalid("classification.rules", "must be a list of tables")
    rules = []
    for index, rule in enumerate(user_rules):
        path = f"classification.rules[{index}]"
        rule = _merge({"provider": "", "model": "", "locality": ""}, rule, path)
        if rule["locality"] not in ("local", "cloud"):
            raise _invalid(f"{path}.locality", "must be local or cloud")
        if not rule["provider"] and not rule["model"]:
            raise _invalid(path, "needs a provider or model pattern")
        rules.append({key: item for key, item in rule.items() if item})
    # User rules come first, so they win over the defaults.
    return {"rules": rules + copy.deepcopy(DEFAULTS["classification"]["rules"])}


def _origins(value: Any) -> dict:
    origins = copy.deepcopy(DEFAULTS["origins"])
    for key, entry in _table(value, "origins").items():
        if key == "non_interactive_sources":
            origins[key] = _scalar([], entry, f"origins.{key}")
        else:
            default = {"interactive": [], "non_interactive": []}
            origins[key] = _merge(default, entry, f"origins.{key}")
    return origins


def _domains(value: Any) -> dict:
    domains: dict[str, dict] = {"projects": {}, "sources": {}}
    for key, mapping in _table(value, "domains").items():
        if key not in domains:
            raise _invalid(f"domains.{key}", "unknown setting")
        for name, domain in _table(mapping, f"domains.{key}").items():
            domains[key][name] = _scalar("", domain, f"domains.{key}.{name}")
    return domains


def _repositories(value: Any) -> dict:
    repositories = {}
    for name, entry in _table(value, "repositories").items():
        path = f"repositories.{name}"
        repository = _merge(_REPOSITORY_KEYS, entry, path)
        if not repository["path"]:
            raise _invalid(f"{path}.path", "is required")
        repositories[name] = repository
    return repositories


def _check_tools(cfg: dict) -> None:
    for role, tools in cfg["tools"].items():
        for tool in tools:
            if any(blocked in tool for blocked in AGENT_LAUNCH_TOOLS):
                raise _invalid(f"tools.{role}", f"{tool} launches other agents (4.4)")
            server, _, name = tool.partition(".")
            if tool not in BUILTIN_TOOLS and not (server and name):
                raise _invalid(
                    f"tools.{role}",
                    f"{tool} is neither a file tool ({', '.join(BUILTIN_TOOLS)}) "
                    "nor <server>.<tool> from the shared MCP definitions",
                )
        if not tools:
            continue
        if role in TOOL_FREE_ROLES:
            raise _invalid(f"tools.{role}", f"must be empty; the {role} gets no tools")
        for name in cfg["roles"][role]["backends"]:
            if cfg["backends"][name]["harness"] == "lmstudio":
                raise _invalid(
                    f"roles.{role}.backends",
                    f"{name} is a direct LM Studio backend, which cannot run the "
                    "tools this role uses; use a harness (5.1)",
                )


def _check_names_file(cfg: dict) -> None:
    raw = cfg["scan"]["names_file"]
    if raw:
        repo = containing_repository(Path(raw).expanduser())
        if repo is not None:
            raise _invalid(
                "scan.names_file",
                f"is inside the git repository {repo}; keep it outside repositories",
            )


def split_target(target: str) -> tuple[str, str]:
    """A currency target's repository name and path."""
    repo, _, path = target.partition(":")
    return repo, path


def _check_currency(cfg: dict) -> None:
    for target in cfg["currency"]["targets"]:
        repo, path = split_target(target)
        parts = PurePosixPath(path).parts
        if repo not in cfg["repositories"]:
            raise _invalid(
                "currency.targets",
                f"{target} must be <repo>:<path> in a configured repository",
            )
        if not parts or path.startswith("/") or ".." in parts or parts[0] == ".git":
            raise _invalid("currency.targets", f"{target} needs a path inside {repo}")


def build_config(raw: dict) -> dict:
    """Validate a parsed config file and merge it over the defaults."""
    raw = dict(raw)
    open_values = {key: raw.pop(key) for key in OPEN_SECTIONS if key in raw}
    fixed = {key: value for key, value in DEFAULTS.items() if key not in OPEN_SECTIONS}
    cfg = _merge(fixed, raw, "")
    cfg["backends"] = _backends(open_values.get("backends", {}))
    cfg["roles"] = _roles(open_values.get("roles", {}), cfg["backends"])
    cfg["classification"] = _classification(open_values.get("classification", {}))
    cfg["origins"] = _origins(open_values.get("origins", {}))
    cfg["domains"] = _domains(open_values.get("domains", {}))
    cfg["repositories"] = _repositories(open_values.get("repositories", {}))
    _check_tools(cfg)
    _check_names_file(cfg)
    _check_currency(cfg)
    return cfg


def load_config(home: Path | None = None) -> dict:
    """Read `config.toml` under the home, or the defaults when it does not exist."""
    path = (home or dreaming_home()) / "config.toml"
    raw: dict = {}
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise DreamingError(
                f"{path}: {exc}", USAGE_ERROR, "invalid_config"
            ) from exc
    return build_config(raw)


def apply_overrides(cfg: dict, overrides: dict) -> dict:
    """A copy of `cfg` with per-run overrides: budget, role backends and usage-limit
    fallback (1.2, 5.1, 5.3)."""
    result = copy.deepcopy(cfg)
    for key, value in overrides.items():
        if key == "budget":
            result["budget"] = _merge(cfg["budget"], value, "budget")
        elif key == "roles":
            for role, names in _table(value, "roles").items():
                if role not in ROLES:
                    raise _invalid(f"roles.{role}", "unknown role", "invalid_override")
                backends = _role_backend_list(names, cfg["backends"], role)
                result["roles"][role] = {"backends": backends}
        elif key == "fallback_on_usage_limit":
            result["thresholds"][key] = _scalar(False, value, key)
        else:
            raise _invalid(key, "cannot be overridden per run", "invalid_override")
    _check_tools(result)
    return result


# Rules applied to sessions


def role_backends(cfg: dict, role: str) -> list[dict]:
    """The role's backends in fallback order, each with its `name`."""
    return [
        {"name": name, **cfg["backends"][name]}
        for name in cfg["roles"][role]["backends"]
    ]


def role_context_tokens(cfg: dict, role: str) -> int:
    """The smallest context in the role's fallback list; the role's inputs are
    sized for it, so any backend in the list can take them (5.2)."""
    return min(backend["context_tokens"] for backend in role_backends(cfg, role))


def classify_locality(cfg: dict, provider: str | None, model: str | None) -> str:
    """`local`, `cloud` or `unknown` from the first matching rule (2.2)."""
    for rule in cfg["classification"]["rules"]:
        checks = (("provider", provider), ("model", model))
        if all(
            key not in rule
            or (value is not None and fnmatchcase(value.lower(), rule[key].lower()))
            for key, value in checks
        ):
            return rule["locality"]
    return "unknown"


def classify_origin(cfg: dict, source: str, origin: str | None) -> tuple[bool, bool]:
    """`(interactive, mapped)` for a session's source and recorded client origin (2.9).

    An unmapped or missing origin counts as interactive; the report lists it.
    """
    origins = cfg["origins"]
    if source in origins["non_interactive_sources"]:
        return False, True
    table = origins.get(source, {})
    if origin is not None:
        if origin in table.get("non_interactive", []):
            return False, True
        if origin in table.get("interactive", []):
            return True, True
    return True, False


def assign_domain(cfg: dict, source: str, project: str | None) -> str | None:
    """The work domain from project mappings (deepest match), then source mappings.

    None means unassigned (2.3).
    """
    if project:
        matches = [
            (len(Path(prefix).parts), domain)
            for prefix, domain in cfg["domains"]["projects"].items()
            if Path(project).is_relative_to(Path(prefix).expanduser())
        ]
        if matches:
            return max(matches)[1]
    return cfg["domains"]["sources"].get(source)


def load_names(cfg: dict) -> list[str]:
    """The private names list for the public-repository scan (9.12)."""
    _check_names_file(cfg)
    raw = cfg["scan"]["names_file"]
    if not raw:
        return []
    lines = Path(raw).expanduser().read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


# Lenses


@dataclass(frozen=True)
class Lens:
    id: str
    version: int
    look_for: str
    may_propose: tuple[str, ...]
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()

    def applies(
        self, domain: str | None, project: str | None, workflow: str | None
    ) -> bool:
        """Every dimension the lens constrains must match; an empty list means any."""
        return all(
            not allowed or value in allowed
            for allowed, value in (
                (self.domains, domain),
                (self.projects, project),
                (self.workflows, workflow),
            )
        )


_LENS_KEYS = {
    "id": "",
    "version": 0,
    "look_for": "",
    "may_propose": [],
    "domains": [],
    "projects": [],
    "workflows": [],
}


def load_lenses(path: Path) -> list[Lens]:
    """Read the lens file (7.1)."""
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise DreamingError(f"{path}: {exc}", USAGE_ERROR, "invalid_lenses") from exc
    if set(doc) - {"lens"} or not isinstance(doc.get("lens", []), list):
        raise _invalid(str(path), "must contain only [[lens]] tables", "invalid_lenses")
    lenses: list[Lens] = []
    for index, entry in enumerate(doc.get("lens", [])):
        where = f"{path} lens {index + 1}"
        try:
            fields = _merge(_LENS_KEYS, entry, where)
        except DreamingError as exc:
            raise DreamingError(exc.message, USAGE_ERROR, "invalid_lenses") from exc
        problem = None
        if not fields["id"]:
            problem = "needs an id"
        elif fields["version"] < 1:
            problem = "needs a version of at least 1"
        elif not fields["look_for"].strip():
            problem = "needs look_for text"
        elif not fields["may_propose"] or set(fields["may_propose"]) - set(PROPOSALS):
            problem = f"may_propose must list some of {', '.join(PROPOSALS)}"
        elif any(lens.id == fields["id"] for lens in lenses):
            problem = f"repeats the id {fields['id']!r}"
        if problem:
            raise _invalid(where, problem, "invalid_lenses")
        lenses.append(
            Lens(
                **{k: tuple(v) if isinstance(v, list) else v for k, v in fields.items()}
            )
        )
    return lenses


# The start-of-run snapshot


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        raise DreamingError(
            f"git {' '.join(args)} in {repo}: {result.stderr.decode().strip()}",
            USAGE_ERROR,
            "invalid_config",
        )
    return result.stdout


def write_snapshot(home: Path, run_id: str, cfg: dict) -> Path:
    """Copy the instruction set from the default branch and the effective config
    into `runs/<id>/snapshot/` (7.3).

    Instructions come from `git show <branch>:instructions/...`, never the working
    tree. The names list stays outside: only its path is part of the config.
    """
    raw_repo = cfg["instructions"]["repo"]
    if not raw_repo:
        raise _invalid("instructions.repo", "is required to start a run")
    repo = Path(raw_repo).expanduser()
    branch = cfg["instructions"]["branch"]
    revision = (
        _git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}").decode().strip()
    )
    listing = _git(
        repo, "ls-tree", "-r", "-z", "--name-only", revision, "--", "instructions"
    )

    snapshot = home / "runs" / run_id / "snapshot"
    snapshot.mkdir(parents=True)
    for name in filter(None, listing.decode().split("\0")):
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git(repo, "show", f"{revision}:{name}"))

    recorded = copy.deepcopy(cfg)
    recorded["instructions"]["revision"] = revision
    (snapshot / "config.json").write_text(
        json.dumps(recorded, indent=2, sort_keys=True)
    )
    return snapshot

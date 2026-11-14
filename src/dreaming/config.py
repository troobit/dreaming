"""Private paths and configuration; no dependency on a harness checkout."""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


class ConfigError(ValueError):
    """Invalid configuration or private storage location (CLI exit 2)."""


def git(repo: Path, *args: str) -> str:
    """Run Git without caller-supplied repository/index/worktree overrides."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"git failed in {repo}: {exc}") from exc
    return result.stdout.strip()


def require_external(path: Path | str) -> Path:
    """Resolve a path and reject storage in ordinary, worktree or bare repositories."""
    resolved = Path(path).expanduser().resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    directory = existing if existing.is_dir() else existing.parent
    for parent in (directory, *directory.parents):
        if (parent / ".git").exists() or (parent / ".git").is_symlink():
            raise ConfigError(f"{resolved} must be outside every git repository")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--absolute-git-dir"],
            env=env,
            capture_output=True,
            timeout=30,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"Cannot check private path with git: {exc}") from exc
    if result.returncode == 0:
        raise ConfigError(f"{resolved} must be outside every git repository")
    if b"not a git repository" not in result.stderr:
        raise ConfigError(f"Cannot determine whether {resolved} is outside git repositories")
    return resolved


def home_path() -> Path:
    return Path(os.environ.get("DREAMING_HOME", "~/.dreaming")).expanduser().resolve()


def ensure_home(path: Path | str | None = None) -> Path:
    home = require_external(path if path is not None else home_path())
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
    except OSError as exc:
        raise ConfigError(f"Cannot create private home {home}: {exc}") from exc
    return home


ROLES = ("reader", "reviewer", "researcher", "author", "replayer")
TOOL_FREE = {"reader", "reviewer", "replayer"}


def agent_tool(name: str) -> bool:
    return bool(
        re.search(r"(?:^|__)(?:agent|task|spawn_agent|(?:codex|gemini)[_-]agent)$", name, re.I)
    )


@dataclass(frozen=True)
class Budget:
    max_sessions: int = 100
    max_seconds: int = 3600
    max_tokens: int = 200_000
    reserve_share: float = 0.25


@dataclass(frozen=True)
class Thresholds:
    min_sessions: int = 3
    min_sessions_for_rate: int = 5
    recurrence_sessions: int = 5
    max_changes_per_pattern: int = 3
    recent_seconds: int = 300
    max_usage_wait: int = 43200
    fallback_on_usage_limit: bool = False
    breaker_k: int = 3
    max_revision_fraction: float = 0.25
    new_cluster_share: float = 0.5
    boost_after_dreams: int = 3
    revalidate_after_days: int = 90
    w_sessions: float = 1.0
    w_recency: float = 1.0
    w_projects: float = 1.0
    half_life_days: int = 30
    replay_min_sessions: int = 2
    replay_max_sessions: int = 5
    replay_max_recurrence: float = 0.25
    currency_min_sources: int = 2
    max_sources: int = 10
    context_warning_share: float = 0.25


@dataclass(frozen=True)
class BackendConfig:
    harness: str = "claude"
    command: str = "claude"
    model: str | None = None
    provider: str | None = None
    context_tokens: int = 200_000
    timeout_seconds: int = 120
    base_url: str = "http://localhost:1234/v1"


@dataclass(frozen=True)
class Lens:
    id: str
    version: str
    look_for: str
    changes: tuple[str, ...]
    domains: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()

    def applies(self, *, domain=None, project=None, workflow=None) -> bool:
        return all(
            not allowed or value in allowed
            for allowed, value in (
                (self.domains, domain),
                (self.projects, project),
                (self.workflows, workflow),
            )
        )


@dataclass(frozen=True)
class Config:
    source: Path
    budget: Budget
    thresholds: Thresholds
    backends: dict[str, BackendConfig]
    roles: dict[str, tuple[str, ...]]
    tools: dict[str, tuple[str, ...]]
    instruction_repo: Path | None = None
    default_branch: str | None = None
    names_file: Path | None = None
    auto_apply: bool = True
    classification: tuple[dict, ...] = ()
    origins: dict = field(default_factory=dict)
    domains: dict = field(default_factory=dict)
    repositories: dict = field(default_factory=dict)
    scan: dict = field(default_factory=dict)
    knowledge_folders: tuple[str, ...] = ()

    def locality(self, provider: str | None, model: str | None) -> str:
        for rule in self.classification:
            if fnmatch.fnmatchcase(
                provider or "", rule.get("provider", "*")
            ) and fnmatch.fnmatchcase(model or "", rule.get("model", "*")):
                return "local" if rule["local"] else "cloud"
        return "unknown"

    def origin(self, source: str, origin: str | None) -> tuple[bool, bool]:
        """Return interactivity and whether an origin mapping was missing."""
        if source in self.origins["non_interactive_sources"]:
            return False, False
        for pattern in self.origins["non_interactive"]:
            if fnmatch.fnmatchcase(origin or "", pattern):
                return False, False
        for pattern in self.origins["interactive"]:
            if fnmatch.fnmatchcase(origin or "", pattern):
                return True, False
        return True, True

    def domain(self, source: str, project: str | None) -> str | None:
        for pattern, domain in self.domains.get("projects", {}).items():
            if project and fnmatch.fnmatchcase(project, pattern):
                return domain
        return self.domains.get("sources", {}).get(source)


def _table(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value


def _strings(value: object, name: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ConfigError(f"{name} must be a list of nonempty strings")
    return tuple(value)


def _settings(cls, raw: dict):
    raw = _table(raw, cls.__name__)
    known = {f.name for f in fields(cls)}
    if raw.keys() - known:
        raise ConfigError(f"Unknown {cls.__name__} settings: {sorted(raw.keys() - known)}")
    value = cls(**raw)
    for key, default in asdict(cls()).items():
        item = getattr(value, key)
        if type(default) is bool:
            valid = type(item) is bool
        elif type(default) is int:
            valid = type(item) is int and item > 0
        elif type(default) is float:
            valid = type(item) in (int, float) and item >= 0 and item < float("inf")
            if key.endswith("share") or key in {"max_revision_fraction", "replay_max_recurrence"}:
                valid = valid and 0 < item < 1
        elif default is None:
            valid = item is None or isinstance(item, str) and bool(item)
        else:
            valid = isinstance(item, str) and bool(item)
        if not valid:
            raise ConfigError(f"Invalid {cls.__name__}.{key}: {item!r}")
    return value


def _merge(base: dict, update: dict) -> dict:
    result = dict(base)
    for key, value in update.items():
        result[key] = (
            _merge(result[key], value)
            if (isinstance(value, dict) and isinstance(result.get(key), dict))
            else value
        )
    return result


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc


def load_config(path: Path | None = None, *, overrides: dict | None = None) -> Config:
    source = require_external(path if path is not None else home_path() / "config.toml")
    raw = _read_toml(source) if source.exists() else {}
    raw = _merge(raw, overrides or {})
    known = {
        "budget",
        "thresholds",
        "backends",
        "roles",
        "tools",
        "instructions",
        "scan",
        "auto_apply",
        "classification",
        "origins",
        "domains",
        "repositories",
        "knowledge_folders",
    }
    if raw.keys() - known:
        raise ConfigError(f"Unknown config settings: {sorted(raw.keys() - known)}")
    backends = {
        "claude": BackendConfig(),
        "codex": BackendConfig(harness="codex", command="codex", context_tokens=262144),
    }
    for name, settings in _table(raw.get("backends", {}), "backends").items():
        settings = _table(settings, f"backends.{name}")
        harness = settings.get("harness", name)
        if harness not in {"claude", "codex", "lmstudio"}:
            raise ConfigError(f"Unknown harness: {harness}")
        backends[name] = _settings(
            BackendConfig, {"harness": harness, "command": harness, **settings}
        )
    roles_raw = _table(raw.get("roles", {}), "roles")
    tools_raw = _table(raw.get("tools", {}), "tools")
    if (roles_raw.keys() | tools_raw.keys()) - set(ROLES):
        raise ConfigError("Unknown role in roles or tools")
    roles = {
        role: _strings(roles_raw.get(role, ["claude"]), f"roles.{role}", nonempty=True)
        for role in ROLES
    }
    tools = {
        role: _strings(
            tools_raw.get(
                role,
                {
                    "researcher": ["Read", "web_search", "web_fetch", "pr_read"],
                    "author": ["Read", "Edit", "Write"],
                }.get(role, []),
            ),
            f"tools.{role}",
        )
        for role in ROLES
    }
    for role, choices in roles.items():
        if any(choice not in backends for choice in choices):
            raise ConfigError(f"Unknown backend for {role}")
        if role in TOOL_FREE and tools[role]:
            raise ConfigError(f"{role} must be tool-free")
        if role not in TOOL_FREE and any(backends[b].harness == "lmstudio" for b in choices):
            raise ConfigError(f"{role} requires a harness for tools")
        if any(agent_tool(tool) for tool in tools[role]):
            raise ConfigError(f"Agent launch tool is not allowed for {role}")
    instructions = _table(raw.get("instructions", {}), "instructions")
    if instructions.keys() - {"repository", "default_branch"}:
        raise ConfigError("Unknown instructions setting")

    def path_setting(value):
        if not isinstance(value, str) or not value:
            raise ConfigError("Paths must be nonempty strings")
        expanded = Path(value).expanduser()
        return (source.parent / expanded).resolve()

    instruction_repo = (
        path_setting(instructions["repository"]) if "repository" in instructions else None
    )
    branch = instructions.get("default_branch")
    if branch is not None and (not isinstance(branch, str) or not branch or branch.startswith("-")):
        raise ConfigError("Invalid instructions.default_branch")
    scan = _table(raw.get("scan", {}), "scan")
    if scan.keys() - {"names_file", "quote_chars", "credential_patterns"}:
        raise ConfigError("Unknown scan setting")
    if type(scan.get("quote_chars", 80)) is not int or scan.get("quote_chars", 80) < 1:
        raise ConfigError("scan.quote_chars must be a positive integer")
    for pattern in _strings(scan.get("credential_patterns", []), "scan.credential_patterns"):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError("Invalid scan credential pattern") from exc
    names = require_external(path_setting(scan["names_file"])) if "names_file" in scan else None
    origins = _merge(
        {
            "non_interactive_sources": ["codex-exec", "lmstudio"],
            "non_interactive": [],
            "interactive": [],
        },
        _table(raw.get("origins", {}), "origins"),
    )
    for key, value in origins.items():
        if key not in {"non_interactive_sources", "non_interactive", "interactive"}:
            raise ConfigError(f"Unknown origin setting: {key}")
        _strings(value, f"origins.{key}")
    rules = _table(raw.get("classification", {}), "classification").get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError("classification.rules must be an array of tables")
    for rule in rules:
        if (
            not isinstance(rule, dict)
            or type(rule.get("local")) is not bool
            or any(not isinstance(rule.get(key, "*"), str) for key in ("provider", "model"))
        ):
            raise ConfigError("Invalid classification rule")
    if type(raw.get("auto_apply", True)) is not bool:
        raise ConfigError("auto_apply must be a boolean")
    domains = _table(raw.get("domains", {}), "domains")
    if domains.keys() - {"projects", "sources"}:
        raise ConfigError("Unknown domain mapping")
    for mapping in domains.values():
        if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
            raise ConfigError("Domain mappings must contain strings")
    repos = _table(raw.get("repositories", {}), "repositories")
    for name, repo in repos.items():
        _table(repo, f"repositories.{name}")
        for command in ("generate", "lint", "test", "deploy"):
            _strings(repo.get(command, []), f"repositories.{name}.{command}")
        if type(repo.get("public", False)) is not bool:
            raise ConfigError(f"repositories.{name}.public must be a boolean")
    return Config(
        source,
        _settings(Budget, raw.get("budget", {})),
        _settings(Thresholds, raw.get("thresholds", {})),
        backends,
        roles,
        tools,
        instruction_repo,
        branch,
        names,
        raw.get("auto_apply", True),
        tuple(rules),
        origins,
        domains,
        repos,
        scan,
        tuple(
            str(path_setting(p))
            for p in _strings(raw.get("knowledge_folders", []), "knowledge_folders")
        ),
    )


def load_lenses(path: Path) -> tuple[Lens, ...]:
    return parse_lenses(_read_toml(path))


def parse_lenses(document: dict) -> tuple[Lens, ...]:
    raw = document.get("lenses", [])
    if not isinstance(raw, list):
        raise ConfigError("lenses must be an array of tables")
    result = []
    for item in raw:
        item = _table(item, "lens")
        for key in ("id", "version", "look_for"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ConfigError(f"Lens needs a nonempty {key}")
        if any(lens.id == item["id"] for lens in result):
            raise ConfigError(f"Duplicate lens id: {item['id']}")
        result.append(
            Lens(
                item["id"],
                item["version"],
                item["look_for"],
                _strings(item.get("changes"), "lens.changes", nonempty=True),
                *(
                    _strings(item.get(key, []), f"lens.{key}")
                    for key in ("domains", "projects", "workflows")
                ),
            )
        )
    return tuple(result)


def read_names(config: Config) -> tuple[str, ...]:
    if config.names_file is None:
        return ()
    path = require_external(config.names_file)
    try:
        return tuple(line.strip() for line in path.read_text().splitlines() if line.strip())
    except OSError as exc:
        raise ConfigError(f"Cannot read private names file: {path}") from exc


def write_snapshot(config: Config, home: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ConfigError("Invalid run id")
    read_names(config)  # Validate access without copying the contents.
    repo = config.instruction_repo
    branch, revision = instruction_revision(config)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def content(*args):
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
                close_fds=True,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Cannot read instruction snapshot: {exc}") from exc

    entries = content("ls-tree", "-rz", revision, "--", "instructions/").split(b"\0")
    blobs = []
    for entry in filter(None, entries):
        meta, name = entry.split(b"\t", 1)
        mode, kind, object_id = meta.decode().split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ConfigError("Instructions must be regular files")
        blobs.append((name.decode(), object_id))
    if not blobs:
        raise ConfigError("No instructions found on the default branch")
    home = ensure_home(home)
    parent = require_external(home / "runs" / run_id)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = parent / "snapshot"
    if destination.exists():
        raise ConfigError(f"Snapshot already exists: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=parent))
    try:
        for name, object_id in blobs:
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(content("cat-file", "blob", object_id))
            target.chmod(0o600)
        (temporary / "config.json").write_text(json.dumps(asdict(config), default=str, indent=2))
        (temporary / "manifest.json").write_text(
            json.dumps(
                {"instruction_revision": revision, "instruction_branch": branch, "run_id": run_id},
                indent=2,
            )
        )
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def instruction_revision(config: Config) -> tuple[str, str]:
    if config.instruction_repo is None:
        raise ConfigError("Configure instructions.repository before starting a run")
    repo = config.instruction_repo
    branch = config.default_branch
    if branch is None:
        for candidate in ("refs/remotes/origin/HEAD", "refs/heads/main", "refs/heads/master"):
            try:
                git(repo, "rev-parse", "--verify", candidate)
                branch = candidate
                break
            except ConfigError:
                continue
    if branch is None:
        raise ConfigError("Cannot determine instruction repository's default branch")
    revision = git(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
    return branch, revision

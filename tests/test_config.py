"""Configuration, lenses and the run snapshot (1.2, 2.2, 2.3, 2.9, 5.x, 7.x)."""

import json
import subprocess
from pathlib import Path

import pytest

from dreaming import cli, config
from dreaming.errors import USAGE_ERROR, DreamingError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_config(isolated_home, text):
    home = isolated_home / ".dreaming"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text)
    return home


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


# Defaults


def test_defaults_apply_without_a_config_file():
    cfg = config.load_config()
    assert cfg["budget"]["sessions"] > 0
    assert cfg["budget"]["tokens"] > 0
    assert cfg["budget"]["wall_clock_minutes"] > 0
    assert 0 < cfg["budget"]["review_author_reserve"] < 1
    assert cfg["thresholds"]["min_sessions"] >= 2
    assert cfg["thresholds"]["max_usage_wait_minutes"] > 0
    assert cfg["thresholds"]["fallback_on_usage_limit"] is False
    assert cfg["adoption"]["auto_apply"] is False


def test_default_tool_lists_give_readers_reviewers_and_replayers_no_tools():
    cfg = config.load_config()
    assert cfg["tools"]["reader"] == []
    assert cfg["tools"]["reviewer"] == []
    assert cfg["tools"]["replayer"] == []
    assert cfg["tools"]["researcher"]
    assert cfg["tools"]["author"]
    every_tool = [tool for tools in cfg["tools"].values() for tool in tools]
    assert not any("codex-agent" in t or "gemini-agent" in t for t in every_tool)


def test_default_classification_and_origin_rules():
    cfg = config.load_config()
    assert config.classify_locality(cfg, "lmstudio", "qwen3") == "local"
    assert config.classify_locality(cfg, None, "claude-opus-5-5") == "cloud"
    assert config.classify_locality(cfg, "openai", "gpt-6") == "cloud"
    assert config.classify_locality(cfg, None, None) == "unknown"
    assert config.classify_locality(cfg, "someone", "mystery") == "unknown"

    assert config.classify_origin(cfg, "codex-exec", None) == (False, True)
    assert config.classify_origin(cfg, "lmstudio", None) == (False, True)
    assert config.classify_origin(cfg, "codex", "codex_exec") == (False, True)
    assert config.classify_origin(cfg, "claude", "sdk-cli") == (False, True)
    assert config.classify_origin(cfg, "claude", "cli") == (True, True)


def test_unmapped_or_missing_origins_count_as_interactive_and_unmapped():
    cfg = config.load_config()
    assert config.classify_origin(cfg, "claude", "brand-new-entrypoint") == (
        True,
        False,
    )
    assert config.classify_origin(cfg, "copilot", None) == (True, False)


# The config file


def test_config_file_values_override_defaults(isolated_home):
    _write_config(
        isolated_home,
        """
[budget]
tokens = 1000

[thresholds]
fallback_on_usage_limit = true
max_usage_wait_minutes = 45

[backends.local]
harness = "lmstudio"
model = "qwen3"
context_tokens = 32768

[roles.reader]
backends = ["local", "claude"]
""",
    )
    cfg = config.load_config()
    assert cfg["budget"]["tokens"] == 1000
    assert cfg["budget"]["sessions"] == config.DEFAULTS["budget"]["sessions"]
    assert cfg["thresholds"]["fallback_on_usage_limit"] is True
    assert cfg["thresholds"]["max_usage_wait_minutes"] == 45
    assert cfg["roles"]["reader"]["backends"] == ["local", "claude"]
    assert config.role_backends(cfg, "reader")[0]["context_tokens"] == 32768


def test_floats_accept_integers(isolated_home):
    _write_config(isolated_home, "[budget]\nreview_author_reserve = 0\n")
    assert config.load_config()["budget"]["review_author_reserve"] == 0.0


def test_custom_rules_mappings_and_repositories(isolated_home):
    _write_config(
        isolated_home,
        """
[[classification.rules]]
model = "gpt-oss-*"
locality = "local"

[origins.claude]
interactive = ["cli"]
non_interactive = ["sdk-cli", "my-bot"]

[domains.projects]
"/work/agreements" = "contracts"

[domains.sources]
lmstudio = "general"

[repositories.tools]
path = "/work/tools"
public = true
lint = ["make lint"]
test = ["make test"]
""",
    )
    cfg = config.load_config()
    assert config.classify_locality(cfg, "openai", "gpt-oss-120b") == "local"
    assert config.classify_origin(cfg, "claude", "my-bot") == (False, True)
    assert config.assign_domain(cfg, "claude", "/work/agreements") == "contracts"
    assert config.assign_domain(cfg, "claude", "/work/agreements/sub") == "contracts"
    assert config.assign_domain(cfg, "lmstudio", None) == "general"
    assert config.assign_domain(cfg, "claude", "/elsewhere") is None
    repo = cfg["repositories"]["tools"]
    assert repo["public"] is True
    assert repo["default_branch"] == "main"
    assert repo["generate"] == [] and repo["deploy"] == []


@pytest.mark.parametrize(
    "text",
    [
        "this is not toml =",
        "[budget]\ntokens = 'many'\n",
        "[budget]\nunknown_limit = 3\n",
        "[thresholds]\nfallback_on_usage_limit = 1\n",
        "[nonsense]\nx = 1\n",
        "[roles.reader]\nbackends = ['missing']\n",
        "[roles.reader]\nbackends = []\n",
        "[roles.dreamer]\nbackends = ['claude']\n",
        "[backends.x]\nharness = 'vim'\ncontext_tokens = 10\n",
        "[backends.x]\nharness = 'claude'\ncontext_tokens = 0\n",
        "[backends.x]\nharness = 'lmstudio'\ncontext_tokens = 8\n"
        "[roles.researcher]\nbackends = ['x']\n",
        "[[classification.rules]]\nprovider = 'x'\nlocality = 'moon'\n",
        "[[classification.rules]]\nlocality = 'local'\n",
        "[repositories.r]\npublic = true\n",
        "[repositories.r]\npath = '/r'\nlint = 'make lint'\n",
        "[tools]\nreader = ['devtools.codex-agent']\n",
    ],
)
def test_invalid_config_is_a_usage_error(isolated_home, text):
    _write_config(isolated_home, text)
    with pytest.raises(DreamingError) as excinfo:
        config.load_config()
    assert excinfo.value.exit_code == USAGE_ERROR
    assert excinfo.value.code == "invalid_config"


def test_invalid_config_exits_2_from_the_cli(isolated_home, capsys):
    _write_config(isolated_home, "[budget]\ntokens = 'many'\n")
    assert cli.main(["config"]) == USAGE_ERROR
    assert "budget.tokens" in capsys.readouterr().err


def test_cli_prints_the_effective_config(capsys):
    assert cli.main(["config"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["thresholds"]["fallback_on_usage_limit"] is False


def test_example_config_is_valid(isolated_home):
    example = REPO_ROOT / "instructions" / "config.example.toml"
    _write_config(isolated_home, example.read_text())
    config.load_config()


# Per-run overrides


def test_start_overrides_for_budget_backends_and_usage_limit_fallback():
    cfg = config.load_config()
    overridden = config.apply_overrides(
        cfg,
        {
            "budget": {"tokens": 500},
            "roles": {"reviewer": ["codex", "claude"]},
            "fallback_on_usage_limit": True,
        },
    )
    assert overridden["budget"]["tokens"] == 500
    assert overridden["roles"]["reviewer"]["backends"] == ["codex", "claude"]
    assert overridden["thresholds"]["fallback_on_usage_limit"] is True
    # The loaded configuration is not modified.
    assert cfg["budget"]["tokens"] != 500


@pytest.mark.parametrize(
    "overrides",
    [
        {"budget": {"tokens": -1}},
        {"budget": {"wishes": 3}},
        {"roles": {"reviewer": ["nowhere"]}},
        {"fallback_on_usage_limit": "yes"},
        {"colour": "blue"},
    ],
)
def test_invalid_overrides_are_usage_errors(overrides):
    cfg = config.load_config()
    with pytest.raises(DreamingError) as excinfo:
        config.apply_overrides(cfg, overrides)
    assert excinfo.value.exit_code == USAGE_ERROR


# Lenses

LENSES = """
[[lens]]
id = "preferences"
version = 2
look_for = "Explicit preferences and corrections"
may_propose = ["knowledge", "change"]

[[lens]]
id = "xcode"
version = 1
look_for = "Xcode build, run and preview workflows"
may_propose = ["change", "learning"]
domains = ["software"]
projects = ["/work/app"]

[[lens]]
id = "spec-process"
version = 1
look_for = "Friction in the specification workflow"
may_propose = ["change"]
workflows = ["starwave"]
"""


def test_lenses_load_with_versions_and_applicability(tmp_path):
    path = tmp_path / "lenses.toml"
    path.write_text(LENSES)
    lenses = {lens.id: lens for lens in config.load_lenses(path)}
    assert lenses["preferences"].version == 2
    assert lenses["preferences"].applies(domain=None, project=None, workflow=None)
    assert lenses["xcode"].applies(
        domain="software", project="/work/app", workflow=None
    )
    assert not lenses["xcode"].applies(
        domain="software", project="/other", workflow=None
    )
    assert not lenses["xcode"].applies(domain=None, project="/work/app", workflow=None)
    assert lenses["spec-process"].applies(domain="x", project="/p", workflow="starwave")
    assert not lenses["spec-process"].applies(domain="x", project="/p", workflow=None)


@pytest.mark.parametrize(
    "text",
    [
        "[[lens]]\nid = 'a'\nversion = 1\nlook_for = 'x'\nmay_propose = ['poems']\n",
        "[[lens]]\nid = 'a'\nversion = 0\nlook_for = 'x'\nmay_propose = ['change']\n",
        "[[lens]]\nid = 'a'\nversion = 1\nlook_for = ''\nmay_propose = ['change']\n",
        "[[lens]]\nversion = 1\nlook_for = 'x'\nmay_propose = ['change']\n",
        "[[lens]]\nid = 'a'\nversion = 1\nlook_for = 'x'\nmay_propose = ['change']\n"
        "[[lens]]\nid = 'a'\nversion = 2\nlook_for = 'y'\nmay_propose = ['change']\n",
        "[[lens]]\nid = 'a'\nversion = 1\nlook_for = 'x'\nmay_propose = ['change']\n"
        "colour = 'red'\n",
        "lens = 3\n",
    ],
)
def test_invalid_lenses_are_usage_errors(tmp_path, text):
    path = tmp_path / "lenses.toml"
    path.write_text(text)
    with pytest.raises(DreamingError) as excinfo:
        config.load_lenses(path)
    assert excinfo.value.exit_code == USAGE_ERROR


# Names list


def test_names_list_is_read_from_a_file_outside_every_repository(isolated_home):
    names = isolated_home / "private" / "names.txt"
    names.parent.mkdir()
    names.write_text("# people\nAlice Example\n\nAcme Pty Ltd\n")
    _write_config(isolated_home, f"[scan]\nnames_file = '{names}'\n")
    cfg = config.load_config()
    assert config.load_names(cfg) == ["Alice Example", "Acme Pty Ltd"]


def test_names_list_inside_a_repository_is_refused(isolated_home, tmp_path):
    repo = tmp_path / "public-repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    names = repo / "names.txt"
    names.write_text("Alice Example\n")
    _write_config(isolated_home, f"[scan]\nnames_file = '{names}'\n")
    with pytest.raises(DreamingError) as excinfo:
        config.load_config()
    assert excinfo.value.exit_code == USAGE_ERROR


def test_no_names_file_means_no_names():
    assert config.load_names(config.load_config()) == []


# Snapshot


@pytest.fixture
def instruction_repo(tmp_path):
    repo = tmp_path / "dreaming-repo"
    (repo / "instructions" / "roles").mkdir(parents=True)
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "instructions" / "lenses.toml").write_text(LENSES)
    (repo / "instructions" / "roles" / "reader.md").write_text("committed reader\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "instructions")
    return repo


def test_snapshot_reads_instructions_from_the_default_branch(
    isolated_home, instruction_repo
):
    # Uncommitted edits and another branch must not reach the run.
    _git(instruction_repo, "checkout", "-q", "-b", "experiment")
    roles = instruction_repo / "instructions" / "roles"
    (roles / "reader.md").write_text("working tree reader\n")
    (roles / "author.md").write_text("untracked author\n")

    names = isolated_home / "names.txt"
    names.write_text("Secret Person\n")
    _write_config(
        isolated_home,
        f"""
[instructions]
repo = "{instruction_repo}"
branch = "main"

[scan]
names_file = "{names}"
""",
    )
    cfg = config.load_config()
    home = config.ensure_home()
    snapshot = config.write_snapshot(home, "run-1", cfg)

    assert snapshot == home / "runs" / "run-1" / "snapshot"
    reader = snapshot / "instructions" / "roles" / "reader.md"
    assert reader.read_text() == "committed reader\n"
    assert not (snapshot / "instructions" / "roles" / "author.md").exists()
    assert (snapshot / "instructions" / "lenses.toml").read_text() == LENSES

    recorded = json.loads((snapshot / "config.json").read_text())
    assert recorded["budget"] == cfg["budget"]
    assert recorded["instructions"]["revision"]

    for path in snapshot.rglob("*"):
        if path.is_file():
            assert "Secret Person" not in path.read_text()

    lenses = config.load_lenses(snapshot / "instructions" / "lenses.toml")
    assert [lens.id for lens in lenses] == ["preferences", "xcode", "spec-process"]


def test_snapshot_requires_an_instruction_repository(isolated_home):
    cfg = config.load_config()
    with pytest.raises(DreamingError) as excinfo:
        config.write_snapshot(config.ensure_home(), "run-1", cfg)
    assert excinfo.value.exit_code == USAGE_ERROR


def test_snapshot_with_a_missing_branch_is_a_usage_error(
    isolated_home, instruction_repo
):
    _write_config(
        isolated_home,
        f'[instructions]\nrepo = "{instruction_repo}"\nbranch = "trunk"\n',
    )
    cfg = config.load_config()
    with pytest.raises(DreamingError) as excinfo:
        config.write_snapshot(config.ensure_home(), "run-1", cfg)
    assert excinfo.value.exit_code == USAGE_ERROR

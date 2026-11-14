import json
import subprocess
import sys

import pytest

from dreaming.config import ConfigError, load_config, load_lenses, read_names, write_snapshot


def test_defaults_and_overrides(tmp_path):
    config = load_config(tmp_path / "absent.toml")
    assert config.budget.max_tokens > 0
    assert 0 < config.budget.reserve_share < 1
    assert config.thresholds.max_usage_wait > 0
    assert config.thresholds.fallback_on_usage_limit is False
    assert config.tools["reader"] == ()
    assert config.roles["researcher"]
    override = load_config(
        tmp_path / "absent.toml",
        overrides={
            "budget": {"max_tokens": 400},
            "roles": {"reader": ["codex"]},
            "thresholds": {"fallback_on_usage_limit": True},
        },
    )
    assert override.budget.max_tokens == 400
    assert override.roles["reader"] == ("codex",)
    assert override.thresholds.fallback_on_usage_limit is True


@pytest.mark.parametrize(
    "content",
    [
        "broken = [",
        "[budget]\nmax_tokens = -1",
        "[budget]\nmax_tokens = true",
        "[budget]\nreserve_share = 1.1",
        "[roles]\nreader = []",
        '[roles]\nreader = ["missing"]',
        '[tools]\nreader = ["Read"]',
        '[tools]\nauthor = ["spawn_agent"]',
        '[tools]\nauthor = ["Task"]',
        '[tools]\nauthor = ["mcp__devtools__codex_agent"]',
        '[tools]\nauthor = ["mcp__x__codex-agent"]',
        '[backends.local]\nharness = "lmstudio"\n[roles]\nauthor = ["local"]',
        '[thresholds]\nfallback_on_usage_limit = "yes"',
        "[budget]\nmax_toknes = 3",
    ],
)
def test_invalid_configuration(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_config_exits_two(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("not valid toml!")
    monkeypatch.setenv("DREAMING_HOME", str(home))
    result = subprocess.run(
        [sys.executable, "-m", "dreaming.cli", "probe", "--dry-run"], text=True, capture_output=True
    )
    assert result.returncode == 2
    assert "config" in result.stderr.lower()


def test_classification_and_origin_mapping(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
[origins]
non_interactive = ["automation-*"]
interactive = ["terminal"]
[domains.projects]
"/work/*" = "software"
[domains.sources]
copilot = "writing"
[[classification.rules]]
provider = "private-server"
model = "local-*"
local = true
""")
    config = load_config(path)
    assert config.locality("private-server", "local-model") == "local"
    assert config.locality("unmapped", "unknown") == "unknown"
    assert config.origin("codex-exec", None) == (False, False)
    assert config.origin("lmstudio", None) == (False, False)
    assert config.origin("claude", "automation-job") == (False, False)
    assert config.origin("copilot", None) == (True, True)
    assert config.domain("claude", "/work/project") == "software"
    assert config.domain("copilot", None) == "writing"
    assert config.domain("new", None) is None


def test_snapshot_reads_default_commit_not_working_tree(tmp_path, instruction_repo):
    repo, git = instruction_repo
    names = tmp_path / "names.txt"
    names.write_text("Private Person\n")
    path = tmp_path / "config.toml"
    path.write_text(f'''[instructions]
repository = "{repo}"
default_branch = "main"
[scan]
names_file = "names.txt"
''')
    config = load_config(path)
    committed = git("rev-parse", "main")
    git("checkout", "-b", "feature")
    (repo / "instructions" / "roles" / "reader.md").write_text("Wrong working tree")
    (repo / "instructions" / "roles" / "untracked.md").write_text("Wrong untracked")
    snapshot = write_snapshot(config, tmp_path / "home", "run-1")
    assert (snapshot / "instructions" / "roles" / "reader.md").read_text() == "Committed reader"
    assert not (snapshot / "instructions" / "roles" / "untracked.md").exists()
    metadata = json.loads((snapshot / "manifest.json").read_text())
    assert metadata["instruction_revision"] == committed
    assert "Private Person" not in "".join(
        p.read_text() for p in snapshot.rglob("*") if p.is_file()
    )
    assert read_names(config) == ("Private Person",)
    lenses = load_lenses(snapshot / "instructions" / "lenses.toml")
    assert lenses[0].version == "1"
    assert lenses[0].applies(domain="software", project="example", workflow="review")
    assert not lenses[0].applies(domain="writing", project="example", workflow="review")
    with pytest.raises(ConfigError, match="already exists"):
        write_snapshot(config, tmp_path / "home", "run-1")


def test_names_inside_repo_rejected_before_snapshot(tmp_path, instruction_repo):
    repo, _ = instruction_repo
    names = repo / "names.txt"
    names.write_text("Private")
    path = tmp_path / "config.toml"
    path.write_text(f'[scan]\nnames_file = "{names}"')
    with pytest.raises(ConfigError):
        load_config(path)
    assert not (tmp_path / "home").exists()


def test_snapshot_rejects_symlink_instruction(tmp_path, instruction_repo):
    repo, git = instruction_repo
    (repo / "instructions" / "leak").symlink_to("/private/file")
    git("add", ".")
    git("commit", "-m", "symlink")
    path = tmp_path / "config.toml"
    path.write_text(f'[instructions]\nrepository = "{repo}"\ndefault_branch = "main"')
    with pytest.raises(ConfigError, match="regular"):
        write_snapshot(load_config(path), tmp_path / "home", "run")
    assert not (tmp_path / "home" / "runs" / "run" / "snapshot").exists()


@pytest.mark.parametrize(
    "content",
    [
        '[[lenses]]\nid="x"\nversion="1"',
        '[[lenses]]\nid="x"\nversion="1"\nlook_for="test"\nchanges=[]',
    ],
)
def test_invalid_lenses(tmp_path, content):
    path = tmp_path / "lenses.toml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_lenses(path)

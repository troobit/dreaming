import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dreaming.config import ConfigError
from dreaming.install import install, prune_versions
from dreaming.launcher import select_version
from dreaming.store import Store


def fake_version(home, revision, message):
    path = home / "versions" / revision
    (path / "bin").mkdir(parents=True)
    python = path / "bin" / "python"
    python.write_text(f'#!/bin/sh\nprintf "%s\\n" "{message}"\n')
    python.chmod(0o755)
    (path / "installation.json").write_text(json.dumps({"revision": revision}))
    return path


def test_launcher_pins_resume_while_new_and_dry_runs_use_current(tmp_path):
    home = tmp_path / "home"
    old = fake_version(home, "old", "old")
    new = fake_version(home, "new", "new")
    (home / "current").symlink_to(new)
    with Store(home) as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,revision,snapshot_path) "
            "VALUES ('r','interrupted','old','snapshot')"
        )
    assert select_version(home, ["run"]) == old
    assert select_version(home, ["resume", "r"]) == old
    assert select_version(home, ["--config", "elsewhere", "run"]) == old
    assert select_version(home, ["run", "--dry-run"]) == new
    with Store(home) as store:
        store.connection.execute("UPDATE runs SET status='complete'")
    assert select_version(home, ["run"]) == new


@pytest.mark.parametrize("status", ["running", "waiting", "partial", "interrupted"])
def test_pruning_retains_every_unfinished_revision(tmp_path, status):
    home = tmp_path / "home"
    old = fake_version(home, "old", "old")
    new = fake_version(home, "new", "new")
    unused = fake_version(home, "unused", "unused")
    (home / "current").symlink_to(new)
    with Store(home) as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,revision) VALUES ('r',?,'old')", (status,)
        )
    assert prune_versions(home) == ["unused"]
    assert old.exists() and new.exists() and not unused.exists()


def test_missing_pinned_version_is_an_error_instead_of_using_current(tmp_path):
    home = tmp_path / "home"
    with Store(home) as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,revision) VALUES ('r','partial','missing')"
        )
    with pytest.raises(ValueError, match="missing"):
        select_version(home, ["resume", "r"])


@pytest.fixture
def source_repo(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy(root / "pyproject.toml", source)
    shutil.copytree(root / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__"))

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-m", "version one")
    return source, git


def test_install_is_immutable_and_launcher_runs_wheel_outside_checkout(tmp_path, source_repo):
    source, git = source_repo
    home, binaries = tmp_path / "home", tmp_path / "bin"
    revision = git("rev-parse", "HEAD")
    version = install(source, home, binaries)
    assert version == home / "versions" / revision
    assert (home / "current").resolve() == version
    assert (binaries / "dream").is_symlink()
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    env["DREAMING_HOME"] = str(home)
    result = subprocess.run(
        [str(binaries / "dream"), "--version"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "dream 0.1.0"
    # New source cannot alter the installed package, even before a second install.
    init = source / "src" / "dreaming" / "__init__.py"
    init.write_text('__version__ = "0.2.0"\n')
    git("add", ".")
    git("commit", "-m", "version two")
    second = install(source, home, binaries)
    assert second != version
    old = subprocess.run(
        [
            str(version / "bin" / "python"),
            "-I",
            "-c",
            "from dreaming import __version__; print(__version__)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert old.stdout.strip() == "0.1.0"
    with Store(home) as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,revision,snapshot_path) "
            "VALUES ('r','partial',?,'snapshot')",
            (revision,),
        )
    assert select_version(home, ["run"]) == version
    assert select_version(home, ["run", "--dry-run"]) == second
    assert json.loads((version / "installation.json").read_text())["revision"] == revision


def test_foreign_launcher_and_dirty_source_are_refused(tmp_path, source_repo):
    source, _ = source_repo
    home, binaries = tmp_path / "home", tmp_path / "bin"
    binaries.mkdir()
    launcher = binaries / "dream"
    launcher.write_text("foreign command")
    with pytest.raises(ConfigError, match="foreign"):
        install(source, home, binaries)
    assert launcher.read_text() == "foreign command"
    assert not home.exists()
    launcher.unlink()
    (source / "untracked").write_text("uncommitted")
    with pytest.raises(ConfigError, match="clean"):
        install(source, home, binaries)
    assert not home.exists()


def test_install_failure_preserves_current_and_removes_incomplete_version(
    tmp_path, source_repo, monkeypatch
):
    source, git = source_repo
    home, binaries = tmp_path / "home", tmp_path / "bin"
    old = fake_version(home, "old", "old")
    (home / "current").symlink_to(old)

    def failed(*args, **kwargs):
        raise ConfigError("fixture failure")

    monkeypatch.setattr("dreaming.install.run_uv", failed)
    with pytest.raises(ConfigError, match="fixture failure"):
        install(source, home, binaries)
    assert (home / "current").resolve() == old
    assert not (home / "versions" / git("rev-parse", "HEAD")).exists()

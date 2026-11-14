import os
import stat
import subprocess
from pathlib import Path

import pytest

from dreaming.config import ConfigError, ensure_home, require_external


def test_private_home(tmp_path, monkeypatch):
    home = tmp_path / "private" / "dreaming"
    monkeypatch.setenv("DREAMING_HOME", str(home))
    assert ensure_home() == home
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    home.chmod(0o755)
    ensure_home()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


@pytest.mark.parametrize("bare", [False, True])
def test_refuses_repository_before_creating_home(tmp_path, bare):
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", *(["--bare"] if bare else []), str(repo)], check=True, capture_output=True
    )
    with pytest.raises(ConfigError, match="outside.*git repository"):
        ensure_home(repo / "nested" / "private")
    assert not (repo / "nested").exists()


def test_symlink_cannot_hide_repository(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ConfigError):
        require_external(link / "names.txt")


def test_worktree_git_file_is_detected(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /missing/worktrees/example\n")
    with pytest.raises(ConfigError):
        ensure_home(worktree / "private")


def test_git_environment_cannot_hide_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-git"))
    with pytest.raises(ConfigError):
        ensure_home(repo / "private")


def test_installed_wheel_runs_without_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(root)],
        check=True,
        capture_output=True,
    )
    venv = tmp_path / "env"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(next(dist.glob("*.whl")))],
        check=True,
        capture_output=True,
    )
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    env["DREAMING_HOME"] = str(tmp_path / "unused")
    result = subprocess.run(
        [str(venv / "bin" / "dream"), "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "dream 0.1.0"
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from dreaming.store import Store; from dreaming.sessions import ApsisClient",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
    )
    assert not (tmp_path / "unused").exists()

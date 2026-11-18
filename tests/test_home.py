"""The private home (6.4) and the CLI entry point (14.4)."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dreaming import cli, config
from dreaming.errors import USAGE_ERROR, DreamingError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_home_defaults_to_dot_dreaming_under_home(isolated_home, monkeypatch):
    monkeypatch.delenv("DREAMING_HOME")
    assert config.dreaming_home() == isolated_home / ".dreaming"


def test_home_is_created_with_mode_0700(isolated_home):
    home = config.ensure_home()
    assert home == isolated_home / ".dreaming"
    assert home.is_dir()
    assert _mode(home) == 0o700


def test_existing_home_is_tightened_to_0700(isolated_home):
    home = isolated_home / ".dreaming"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    config.ensure_home()
    assert _mode(home) == 0o700


def test_home_inside_a_git_repository_is_refused(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = repo / "nested" / "dreaming-home"
    monkeypatch.setenv("DREAMING_HOME", str(target))
    with pytest.raises(DreamingError) as excinfo:
        config.ensure_home()
    assert excinfo.value.exit_code == USAGE_ERROR
    assert str(repo) in excinfo.value.message
    assert not target.exists()


def test_home_inside_a_worktree_is_refused(tmp_path, monkeypatch):
    # A linked worktree has a `.git` file rather than a directory.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    monkeypatch.setenv("DREAMING_HOME", str(worktree / "home"))
    with pytest.raises(DreamingError):
        config.ensure_home()


def test_cli_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "dream 0.1.0"


def test_package_imports_nothing_from_agentic_coding():
    forbidden = ("scripts", "review_html", "agentic_coding", "screw")
    for path in (REPO_ROOT / "src" / "dreaming").rglob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                module = stripped.split()[1]
                assert not module.startswith(forbidden), f"{path}: {stripped}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_dream_version_runs_from_an_installed_wheel(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(REPO_ROOT)],
        check=True,
        capture_output=True,
    )
    (wheel,) = dist.glob("dreaming-*.whl")
    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(venv / "bin" / "python"),
            str(wheel),
        ],
        check=True,
        capture_output=True,
    )

    # Run from a directory outside every checkout with a minimal environment,
    # so nothing but the installed wheel can supply the package.
    outside = tmp_path / "outside"
    outside.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [str(venv / "bin" / "dream"), "--version"],
        cwd=outside,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "dream 0.1.0"

    located = subprocess.run(
        [
            str(venv / "bin" / "python"),
            "-c",
            "import dreaming; print(dreaming.__file__)",
        ],
        cwd=outside,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    module_path = Path(located.stdout.strip()).resolve()
    assert module_path.is_relative_to(venv.resolve())
    assert not module_path.is_relative_to(REPO_ROOT)
    assert "agentic-coding" not in os.fspath(module_path)

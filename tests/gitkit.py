"""Temporary git repositories for adoption tests."""

import subprocess
from pathlib import Path

from runkit import git


def out(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(root: Path, name: str, files: dict[str, str] | None = None) -> Path:
    """A repository on `main` with one commit holding `files`."""
    repo = root / name
    repo.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    for path, text in (files or {"README.md": f"# {name}\n"}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def commit(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", message)
    return out(repo, "rev-parse", "HEAD")


def head(repo: Path, ref: str = "HEAD") -> str:
    return out(repo, "rev-parse", ref)


def branches(repo: Path) -> list[str]:
    return out(repo, "branch", "--format=%(refname:short)").split()


def status(repo: Path) -> str:
    """The checkout's porcelain status, empty when clean."""
    return out(repo, "status", "--porcelain")

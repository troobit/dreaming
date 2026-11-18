"""Versioned installs and the launcher (7.3, Decision 11)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from runkit import fake_config, git

from dreaming import run, store, versions
from dreaming.errors import DreamingError

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_version(home, revision):
    """A version directory whose `dream` prints its revision and arguments, and
    whose `python` is this interpreter, so the launcher module can run."""
    bin_dir = home / "versions" / revision / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "dream").write_text(f'#!/bin/sh\necho "{revision} $*"\n')
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    for program in bin_dir.iterdir():
        program.chmod(0o755)
    (home / "versions" / revision / versions.REVISION_FILE).write_text(revision)
    return bin_dir.parent


def _point_current(home, revision):
    current = home / "current"
    current.unlink(missing_ok=True)
    current.symlink_to(Path("versions") / revision)


def _add_run(home, run_id, revision, status):
    conn = store.open_store(home / "dreaming.db")
    run.create_run(conn, run_id, fake_config(), status=status, revision=revision)
    conn.close()


@pytest.fixture
def two_versions(home):
    _fake_version(home, "aaaa")
    _fake_version(home, "bbbb")
    _point_current(home, "bbbb")
    return home


def test_a_new_run_uses_current(two_versions):
    home = two_versions
    assert versions.target(home) == home / "current"
    _add_run(home, "dream-done", "aaaa", "complete")
    assert versions.target(home) == home / "current"


@pytest.mark.parametrize("status", ["running", "waiting", "interrupted"])
def test_an_unfinished_run_uses_the_revision_it_started_on(two_versions, status):
    home = two_versions
    _add_run(home, "dream-1", "aaaa", status)
    assert versions.target(home) == home / "versions" / "aaaa"


def test_a_missing_recorded_version_falls_back_to_current(two_versions):
    # The program itself then refuses to resume on another revision.
    home = two_versions
    _add_run(home, "dream-1", "gone", "interrupted")
    assert versions.target(home) == home / "current"


def test_the_installed_launcher_execs_the_chosen_version(two_versions):
    home = two_versions
    launcher = versions.write_launcher(home)

    def dream(*args):
        result = subprocess.run(
            [str(launcher), *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    assert dream("status", "--plan") == "bbbb status --plan"
    _add_run(home, "dream-1", "aaaa", "interrupted")
    assert dream("run") == "aaaa run"


def test_pruning_keeps_current_and_every_version_an_unfinished_run_records(home):
    for revision in ("old", "running", "waiting", "current"):
        _fake_version(home, revision)
    _point_current(home, "current")
    _add_run(home, "dream-1", "running", "running")
    _add_run(home, "dream-2", "waiting", "waiting")
    _add_run(home, "dream-3", "old", "complete")
    assert versions.prune(home) == ["old"]
    assert sorted(p.name for p in (home / "versions").iterdir()) == [
        "current",
        "running",
        "waiting",
    ]


def test_install_refuses_to_overwrite_a_foreign_dream_command(home, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    foreign = bin_dir / "dream"
    foreign.symlink_to("/opt/elsewhere/dream")
    with pytest.raises(DreamingError) as excinfo:
        versions.install(REPO_ROOT, home, bin_dir)
    assert str(foreign) in excinfo.value.message
    assert excinfo.value.code == "foreign_file"
    assert foreign.readlink() == Path("/opt/elsewhere/dream")
    assert not (home / "versions").exists()


def _source_repo(tmp_path, version="0.1.0"):
    """A committed copy of this package, so the install builds the working tree."""
    repo = tmp_path / "source"
    if not repo.exists():
        repo.mkdir()
        shutil.copy(REPO_ROOT / "pyproject.toml", repo)
        shutil.copytree(
            REPO_ROOT / "src",
            repo / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        git(tmp_path, "init", "-q", "-b", "main", str(repo))
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
    init = repo / "src" / "dreaming" / "__init__.py"
    text = init.read_text().split('__version__ = "')[0]
    init.write_text(f'{text}__version__ = "{version}"\n')
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--allow-empty", "-m", f"version {version}")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return repo, head.stdout.strip()


def _dream_version(bin_dir):
    result = subprocess.run(
        [str(bin_dir / "dream"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_an_install_does_not_change_the_version_an_unfinished_run_uses(home, tmp_path):
    bin_dir = tmp_path / "local-bin"
    source, first = _source_repo(tmp_path, "0.1.0")
    versions.install(source, home, bin_dir)

    version = home / "versions" / first
    assert (version / versions.REVISION_FILE).read_text().strip() == first
    assert (home / "current").resolve() == version.resolve()
    assert (bin_dir / "dream").readlink() == home / "bin" / "dream"
    assert _dream_version(bin_dir) == "dream 0.1.0"
    recorded = subprocess.run(
        [
            str(version / "bin" / "python"),
            "-c",
            "from dreaming import versions; print(versions.revision())",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert recorded.stdout.strip() == first

    # A dream starts on the first version, then dreaming's code changes.
    _add_run(home, "dream-1", first, "interrupted")
    source, second = _source_repo(tmp_path, "0.2.0")
    versions.install(source, home, bin_dir)
    assert (home / "current").resolve() == (home / "versions" / second).resolve()
    assert version.is_dir()
    assert _dream_version(bin_dir) == "dream 0.1.0"

    # Once the run is finished, new invocations get the new version and the
    # next install removes the old one.
    conn = store.open_store(home / "dreaming.db")
    conn.execute("UPDATE runs SET status = 'complete'")
    conn.close()
    assert _dream_version(bin_dir) == "dream 0.2.0"
    versions.install(source, home, bin_dir)
    assert not version.exists()

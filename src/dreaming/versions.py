"""Versioned installs: a run finishes on the program version it started with (7.3,
Decision 11).

`make install` builds the committed revision of the checkout into
`~/.dreaming/versions/<revision>/`, points `current` at it and links
`~/.local/bin/dream` to a launcher. The launcher runs the version an unfinished
run started on, so a resume uses it, and `current` otherwise. While a run is
unfinished every invocation goes to its version, which also keeps a newer
version from migrating the store under it. A version is removed only when it is
not current and no unfinished run records it.

    python -m dreaming.versions install [--source DIR]
    python -m dreaming.versions launch ARGS...   (what the launcher runs)
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from dreaming import config, store
from dreaming.errors import USAGE_ERROR, DreamingError

REVISION_FILE = "dreaming-revision"

# `-I` keeps the working directory off `sys.path`, so a checkout there cannot
# shadow the installed package.
LAUNCHER = """#!/bin/sh
# Written by `make install` in dreaming: runs the version a dream needs (7.3).
home="${DREAMING_HOME:-$HOME/.dreaming}"
exec "$home/current/bin/python" -I -m dreaming.versions launch "$@"
"""


def revision() -> str:
    """The revision of the running program, or `dev` when it is not an install."""
    marker = Path(sys.prefix) / REVISION_FILE
    return marker.read_text().strip() if marker.is_file() else "dev"


def unfinished_revisions(home: Path) -> list[str]:
    """Revisions recorded by unfinished runs, newest run first."""
    path = store.store_path(home)
    if not path.exists():
        return []
    conn = store.connect(path)
    try:
        rows = conn.execute(
            f"SELECT revision FROM runs WHERE status IN {store.UNFINISHED_RUN_STATUSES}"
            " ORDER BY started_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def target(home: Path) -> Path:
    """The version to run: the one the unfinished run started on, else current."""
    for recorded in unfinished_revisions(home)[:1]:
        version = home / "versions" / recorded
        if (version / "bin" / "dream").exists():
            return version
    return home / "current"


def launch(args: list[str]) -> None:
    program = target(config.dreaming_home()) / "bin" / "dream"
    os.execv(program, [str(program), *args])


def prune(home: Path) -> list[str]:
    """Remove versions that are neither current nor recorded by an unfinished run."""
    root = home / "versions"
    if not root.is_dir():
        return []
    keep = set(unfinished_revisions(home))
    current = home / "current"
    if current.exists():
        keep.add(current.resolve().name)
    removed = []
    for version in sorted(root.iterdir()):
        if version.name not in keep:
            shutil.rmtree(version)
            removed.append(version.name)
    return removed


def write_launcher(home: Path) -> Path:
    launcher = home / "bin" / "dream"
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    return launcher


def _run(argv: list[str]) -> bytes:
    result = subprocess.run(argv, capture_output=True)
    if result.returncode != 0:
        raise DreamingError(f"{' '.join(argv)}: {result.stderr.decode().strip()}")
    return result.stdout


def _build(source: Path, rev: str, version: Path) -> None:
    """Install the committed tree at `rev`, not the working tree, so the version
    directory holds exactly that revision."""
    archive = _run(["git", "-C", str(source), "archive", "--format=tar", rev])
    shutil.rmtree(version, ignore_errors=True)
    with tempfile.TemporaryDirectory() as tree:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(tree, filter="data")
        _run(["uv", "venv", "--quiet", "--python", sys.executable, str(version)])
        python = version / "bin" / "python"
        _run(["uv", "pip", "install", "--quiet", "--python", str(python), tree])
    # Written last: a version directory without it is incomplete and rebuilt.
    (version / REVISION_FILE).write_text(rev + "\n")


def install(source: Path, home: Path, bin_dir: Path) -> Path:
    """Install the checkout's HEAD as a version and make it current."""
    launcher = home / "bin" / "dream"
    link = bin_dir / "dream"
    ours = link.is_symlink() and link.readlink() == launcher
    if not ours and (link.exists() or link.is_symlink()):
        raise DreamingError(
            f"{link} exists and is not dreaming's launcher; remove it (for a `uv tool`"
            " install, run `uv tool uninstall dreaming`) and install again",
            USAGE_ERROR,
            "foreign_file",
        )
    head = _run(["git", "-C", str(source), "rev-parse", "--short=12", "HEAD"])
    rev = head.decode().strip()
    version = home / "versions" / rev
    if not (version / REVISION_FILE).is_file():
        _build(source, rev, version)
    write_launcher(home)
    staged = home / "current.new"
    staged.unlink(missing_ok=True)
    staged.symlink_to(Path("versions") / rev)
    os.replace(staged, home / "current")
    if not ours:
        bin_dir.mkdir(parents=True, exist_ok=True)
        link.symlink_to(launcher)
    removed = prune(home)
    print(f"installed {rev} in {version}; {link} runs it for new dreams")
    if removed:
        print(f"removed versions no run needs: {', '.join(removed)}")
    return version


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["launch"]:
        launch(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m dreaming.versions")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="install the checkout's HEAD")
    setup.add_argument("--source", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        install(args.source, config.ensure_home(), Path.home() / ".local" / "bin")
    except DreamingError as exc:
        print(f"dream install: {exc.message}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Immutable revision installs and explicit pruning of unused versions."""

import argparse
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from dreaming.config import ConfigError, ensure_home, git, home_path, require_external
from dreaming.run import RunLock
from dreaming.store import Store


def run_uv(*arguments: str) -> None:
    try:
        subprocess.run(
            ["uv", *arguments],
            check=True,
            capture_output=True,
            text=True,
            close_fds=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"Installation failed: {exc.stderr or exc}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"Installation failed: {exc}") from exc


@contextmanager
def installation_lock(home: Path):
    fd = os.open(require_external(home / "install-lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def replace_link(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_launcher(home: Path, binaries: Path) -> None:
    entry = binaries / "dream"
    if (entry.exists() or entry.is_symlink()) and (
        not entry.is_symlink() or entry.resolve() != (home / "launcher.py").resolve()
    ):
        raise ConfigError(f"Refusing to overwrite foreign command: {entry}")
    launcher = home / "launcher.py"
    if (launcher.exists() or launcher.is_symlink()) and (
        launcher.is_symlink() or "Dreaming version launcher" not in launcher.read_text()[:200]
    ):
        raise ConfigError(f"Refusing to overwrite foreign launcher: {launcher}")
    current = home / "current"
    if (current.exists() or current.is_symlink()) and (
        not current.is_symlink() or current.resolve().parent != (home / "versions").resolve()
    ):
        raise ConfigError(f"Refusing to overwrite foreign current path: {current}")


def install(source: Path, home: Path, binaries: Path) -> Path:
    source, home, binaries = (
        source.resolve(),
        require_external(home),
        binaries.expanduser().absolute(),
    )
    check_launcher(home, binaries)
    if git(source, "status", "--porcelain", "--untracked-files=normal"):
        raise ConfigError("Versioned installation requires a clean committed source tree")
    revision = git(source, "rev-parse", "HEAD")
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", revision],
        check=True,
        capture_output=True,
        close_fds=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).stdout
    home = ensure_home(home)
    with installation_lock(home):
        check_launcher(home, binaries)
        versions = require_external(home / "versions")
        versions.mkdir(mode=0o700, exist_ok=True)
        version = versions / revision
        if version.is_symlink():
            raise ConfigError(f"Refusing a linked version directory: {version}")
        manifest = version / "installation.json"
        if version.exists():
            if (
                not manifest.is_file()
                or json.loads(manifest.read_text()).get("revision") != revision
                or not (version / "bin" / "python").is_file()
            ):
                raise ConfigError(f"Incomplete or foreign version directory: {version}")
        else:
            version.mkdir(mode=0o700)
            try:
                with tempfile.TemporaryDirectory(prefix="dreaming-install-") as directory:
                    committed = Path(directory)
                    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                        bundle.extractall(committed, filter="data")
                    run_uv("venv", "--python", "3.12", str(version))
                    run_uv(
                        "pip",
                        "install",
                        "--python",
                        str(version / "bin" / "python"),
                        str(committed),
                    )
                manifest.write_text(json.dumps({"revision": revision, "source": str(source)}))
                manifest.chmod(0o600)
            except BaseException:
                shutil.rmtree(version)
                raise
        # Install only the standalone bootstrap; it must not import whichever package is current.
        launcher = home / "launcher.py"
        temporary = home / f".launcher-{uuid.uuid4().hex}"
        try:
            temporary.write_text(files("dreaming").joinpath("launcher.py").read_text())
            temporary.chmod(0o700)
            os.replace(temporary, launcher)
        finally:
            temporary.unlink(missing_ok=True)
        binaries.mkdir(parents=True, exist_ok=True)
        replace_link(home / "current", version)
        replace_link(binaries / "dream", launcher)
        return version


def prune_versions(home: Path) -> list[str]:
    home = ensure_home(home)
    # Prevent a new run from selecting a version while its directory is being removed.
    with RunLock(home), installation_lock(home), Store.readonly(home) as store:
        retained = {
            row[0]
            for row in store.connection.execute(
                "SELECT revision FROM runs "
                "WHERE status IN ('running','waiting','interrupted','partial')"
            )
        }
        retained.add((home / "current").resolve().name)
        removed = []
        versions = require_external(home / "versions")
        for version in sorted(versions.glob("*")):
            if version.is_dir() and not version.is_symlink() and version.name not in retained:
                manifest = version / "installation.json"
                if (
                    manifest.is_file()
                    and json.loads(manifest.read_text()).get("revision") == version.name
                ):
                    shutil.rmtree(version)
                    removed.append(version.name)
        return removed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin").expanduser())
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.prune:
            print(json.dumps({"removed": prune_versions(home_path())}))
        else:
            print(install(args.source, home_path(), args.bin_dir))
        return 0
    except (ConfigError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"dream install: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

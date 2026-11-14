#!/usr/bin/env python3
"""Dreaming version launcher: standalone stdlib bootstrap, copied during installation."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


def select_version(home, arguments, request=None):
    arguments = list(arguments)
    if "--config" in arguments:
        index = arguments.index("--config")
        del arguments[index : index + 2]
    arguments = [arg for arg in arguments if not arg.startswith("--config=")]
    command = arguments[0] if arguments else ""
    operation = (request or {}).get("op") if command == "api" else None
    pin = command in {"start", "run", "resume", "discard"} and "--dry-run" not in arguments
    pin |= operation in {"dream.start", "dream.resume", "dream.discard"}
    revision = None
    database = home / "dreaming.db"
    if pin and database.exists():
        # Inspection here must precede importing the current package or running migrations.
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            identifier = (request or {}).get("run_id")
            if command in {"resume", "discard"} and len(arguments) > 1:
                identifier = arguments[1]
            where = "status IN ('running','waiting','partial','interrupted') "
            where += "AND snapshot_path IS NOT NULL"
            params = ()
            if identifier:
                where = "id=?"
                params = (identifier,)
            row = conn.execute(
                f"SELECT revision FROM runs WHERE {where} ORDER BY rowid DESC LIMIT 1", params
            ).fetchone()
            if row:
                revision = row[0]
        finally:
            conn.close()
    if revision is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", revision):
            raise ValueError("Invalid recorded program revision")
        version = home / "versions" / revision
    else:
        version = (home / "current").resolve()
    if not (version / "bin" / "python").is_file():
        raise ValueError(
            f"Program version missing: {revision or 'current'}; restore its installation"
        )
    return version


def main():
    home = Path(os.environ.get("DREAMING_HOME", "~/.dreaming")).expanduser().resolve()
    arguments = sys.argv[1:]
    body = None
    request = None
    if "api" in arguments:
        body = sys.stdin.buffer.read()
        try:
            request = json.loads(body)
            if not isinstance(request, dict):
                request = None
        except ValueError:
            pass  # The selected CLI owns API validation.
    try:
        version = select_version(home, arguments, request)
        command = [str(version / "bin" / "python"), "-I", "-m", "dreaming.cli", *arguments]
        env = dict(os.environ, DREAMING_REVISION=version.name)
        if body is not None:
            return subprocess.run(command, input=body, env=env, close_fds=True).returncode
        os.execve(command[0], command, env)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"dream: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

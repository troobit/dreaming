"""Builds a synthetic home for apsis and records its shared-json output.

The inputs under fixtures/apsis/inputs/ are synthetic, adapted from the synthetic
transcript fixtures in orbit's internal/transcript/testdata (see the README
beside them). To re-record the contract fixtures after an apsis upgrade:

    uv run python tests/apsis_contract.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "apsis"
INPUTS = FIXTURES / "inputs"
SOURCES = ("claude", "codex", "codex-exec", "copilot", "kiro", "lmstudio")
NORMALISED_ROOT = "/fixture-root"


def build_home(root: Path) -> list[str]:
    """Lay the inputs out where apsis looks for them under `root/home`, and return
    the extra apsis arguments for the exec-stream and LM Studio log roots."""
    home = root / "home"
    claude = home / ".claude" / "projects" / "-work-example-repo"
    claude.mkdir(parents=True)
    for path in (INPUTS / "claude").iterdir():
        shutil.copy(path, claude / path.name)
    for path in (INPUTS / "codex").iterdir():
        day = path.name.split("-")[1:4]
        target = home / ".codex" / "sessions" / day[0] / day[1] / day[2][:2]
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, target / path.name)
    shutil.copytree(INPUTS / "copilot", home / ".copilot" / "session-state")
    shutil.copytree(INPUTS / "codex-exec", root / "codex-exec")
    logs = root / "lmstudio" / "2026-03"
    logs.mkdir(parents=True)
    shutil.copy(INPUTS / "lmstudio" / "2026-03-05.1.log", logs)
    return [
        "--codex-exec-dir",
        str(root / "codex-exec"),
        "--lmstudio-log-dir",
        str(root / "lmstudio"),
    ]


def apsis_env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("APSIS_")}
    return {**env, "HOME": str(root / "home"), "TZ": "UTC"}


def _run(apsis: str, args: list[str], root: Path) -> dict:
    result = subprocess.run(
        [apsis, *args],
        env=apsis_env(root),
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _normalise(doc: dict, root: Path) -> dict:
    text = json.dumps(doc)
    for spelling in {str(root), str(root.resolve())}:
        text = text.replace(spelling, NORMALISED_ROOT)
    return json.loads(text)


def record(apsis: str, root: Path) -> dict[str, dict]:
    """The listing and one session document per source, keyed by file stem."""
    extra = build_home(root)
    listing = _run(
        apsis, ["--all-projects", "--list", "-f", "shared-json", *extra], root
    )
    docs = {"listing": listing}
    for row in listing["sessions"]:
        source = row["source"]
        name = f"session-{source}"
        if source == "codex" and row.get("provider") == "lmstudio":
            name = "session-codex-lmstudio"
        if source == "lmstudio" and not row.get("thread_id"):
            continue
        if name in docs:
            continue
        args = ["--all-projects", "-s", source, "-f", "shared-json", *extra, row["id"]]
        docs[name] = _run(apsis, args, root)
    kiro = INPUTS / "kiro" / "basic.json"
    docs["session-kiro"] = _run(
        apsis, ["-a", "kiro", "-f", "shared-json", str(kiro)], root
    )
    return {name: _normalise(doc, root) for name, doc in docs.items()}


def main() -> int:
    apsis = shutil.which("apsis")
    if apsis is None:
        print("apsis is not on PATH", file=sys.stderr)
        return 1
    version = subprocess.run([apsis, "--version"], capture_output=True, text=True)
    with tempfile.TemporaryDirectory() as tmp:
        docs = record(apsis, Path(tmp))
    for name, doc in docs.items():
        (FIXTURES / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n")
    (FIXTURES / "APSIS_VERSION").write_text(version.stdout)
    print(f"recorded {', '.join(sorted(docs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

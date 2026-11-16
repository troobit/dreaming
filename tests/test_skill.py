import re
import shlex
from pathlib import Path

import pytest

SKILL = Path(__file__).parent.parent / "skills" / "dream" / "SKILL.md"
SUBCOMMANDS = {"start", "run", "status", "resume", "api"}


def frontmatter(text):
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "missing frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def commands(text):
    blocks = re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def test_skill_has_valid_frontmatter():
    meta = frontmatter(SKILL.read_text())
    assert meta["name"] == "dream"
    assert meta["description"].strip()
    assert meta["allowed-tools"] == "Bash(dream:*)"


def test_skill_invokes_only_the_installed_command():
    found = commands(SKILL.read_text())
    assert found
    for line in found:
        pipeline = [part.strip() for part in line.split("|")]
        command = shlex.split(pipeline[-1])
        assert command[0] == "dream", line
        assert command[1] in SUBCOMMANDS, line
        if command[1] == "api":
            assert len(pipeline) == 2 and pipeline[0].startswith("echo '"), line
        else:
            assert len(pipeline) == 1, line


def test_skill_covers_start_dry_run_inspect_resume_and_report():
    text = SKILL.read_text()
    joined = "\n".join(commands(text))
    for expected in ("dream start", "--dry-run", "dream status", "dream resume", "report_path"):
        assert expected in joined or expected in text
    assert "--requester" not in joined  # requester defaults to user
    assert "any directory" in text


def test_skill_directory_is_self_contained():
    assert {p.name for p in SKILL.parent.iterdir()} == {"SKILL.md"}


@pytest.mark.parametrize("path", [SKILL])
def test_no_repository_relative_paths(path):
    assert "./" not in path.read_text().replace("../", "")

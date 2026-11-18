"""The `dream` agent skill (1.1, 10.1, 14.1).

The skill reaches the program only through the installed `dream` command and
`dream api`, so it works from any directory and other harness repositories can
install it by copying its folder (design, Placement).
"""

import json
import re
import shlex
from pathlib import Path

import pytest

from dreaming import api, cli

SKILL = Path(__file__).parents[1] / "skills" / "dream" / "SKILL.md"
BLOCK = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _parts():
    text = SKILL.read_text()
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match, "SKILL.md must open with front matter between --- lines"
    return match.group(1), match.group(2)


def _front_matter():
    fields = {}
    for line in _parts()[0].splitlines():
        key, sep, value = line.partition(":")
        assert sep and key.strip() and value.strip(), f"not `key: value`: {line!r}"
        fields[key.strip()] = value.strip()
    return fields


def _blocks(language):
    return [body for lang, body in BLOCK.findall(_parts()[1]) if lang == language]


def _commands():
    return [
        shlex.split(line)
        for body in _blocks("sh")
        for line in body.splitlines()
        if line.strip()
    ]


def _requests():
    return [json.loads(body) for body in _blocks("json")]


def test_the_skill_has_valid_front_matter():
    fields = _front_matter()
    assert fields["name"] == SKILL.parent.name == "dream"
    assert len(fields["description"]) > 40
    assert set(fields) <= {"name", "description"}


def test_every_command_is_the_installed_dream_command():
    commands = _commands()
    assert commands
    for argv in commands:
        assert argv[0] == "dream", f"runs something other than dream: {argv}"
        args = cli._parser().parse_args(argv[1:])
        assert args.command is not None


def test_every_request_is_a_dream_api_request():
    requests = _requests()
    assert requests
    for request in requests:
        assert request["dreaming_api"] == api.VERSION
        assert request["op"] in api.OPS
    assert any(r["op"] == "dream.status" for r in requests)


def test_the_skill_starts_dry_runs_scopes_inspects_resumes_and_opens_the_report():
    used = {(argv[1], arg) for argv in _commands() for arg in argv[2:]}
    used |= {(argv[1], None) for argv in _commands()}
    for wanted in (
        ("run", None),
        ("run", "--dry-run"),
        ("run", "--repo"),
        ("run", "--workflow"),
        ("status", None),
        ("resume", None),
        ("report", "--open"),
    ):
        assert wanted in used, (
            f"the skill never runs dream {' '.join(filter(None, wanted))}"
        )


def test_the_dream_is_requested_by_the_user():
    for argv in _commands():
        assert "--requester" not in argv
        assert "--wait" not in argv, "the dream must keep running without the session"
    for request in _requests():
        assert request.get("requester", "user") == "user"


@pytest.mark.parametrize("word", ["uv run", "python", "make ", "src/", "cd "])
def test_the_skill_needs_no_checkout(word):
    commands = " ".join(" ".join(argv) for argv in _commands())
    assert word not in commands

"""A scripted model and helpers for running Read and Review steps in tests."""

import json
from pathlib import Path

from runkit import FakeClock, LocalPhase, add_run, make_runner

from dreaming import config, observe, review, roles
from dreaming.backends import CallResult

INSTRUCTIONS = Path(__file__).parents[1] / "instructions"


def build_config(context_tokens=100_000, **sections):
    return config.build_config(
        {
            "backends": {
                "fake": {"harness": "claude", "context_tokens": context_tokens}
            },
            "roles": {role: {"backends": ["fake"]} for role in config.ROLES},
            **sections,
        }
    )


def instructions(cfg):
    return roles.load(INSTRUCTIONS, cfg)


def prompt_input(prompt):
    """The data a prompt carries between its markers."""
    start = prompt.index(roles.INPUT_START) + len(roles.INPUT_START)
    end = prompt.rindex(roles.INPUT_END)
    return json.loads(prompt[start:end])


class ScriptedModel:
    """Answers each call with `answer(role, data)`, where `data` is the call's
    input; a callable answer may also raise."""

    name = "fake"
    release_date = ""
    accepts_session_id = False

    def __init__(self, answer, *, context_tokens=100_000, before=None):
        self.answer = answer
        self.context_tokens = context_tokens
        self.before = before
        self.inputs = []

    def call(self, role, prompt, schema, tools, session_id=None):
        data = prompt_input(prompt)
        if self.before:
            self.before(role, data)
        self.inputs.append((role, data))
        return CallResult(
            self.answer(role, data),
            100,
            20,
            session_ref=f"fake-session-{len(self.inputs)}",
            model="fake-model",
        )

    def of(self, role):
        return [data for called, data in self.inputs if called == role]


def observation(entry, quote, key, **fields):
    return {
        "entry_id": entry["id"],
        "quote": quote,
        "kind": "user_statement" if entry["role"] == "user" else "tool_result",
        "modality": "asserted",
        "outcome": "unknown",
        "outcome_entry_id": None,
        "lens": "preferences",
        "pattern_key": key,
        **fields,
    }


def quoting_users(data):
    """A reader that quotes every user entry under the preferences lens, keyed by
    the text after `key:` when present."""
    found = []
    for entry in data["entries"]:
        if entry["role"] == "user":
            text = entry["text"]
            key = text.split("key:", 1)[1].strip() if "key:" in text else text
            found.append(observation(entry, text, key))
    return {"observations": found}


EMPTY_REVIEW = {
    "assignments": [],
    "pattern_merges": [],
    "knowledge": [],
    "item_merges": [],
    "candidates": [],
    "questions": [],
}


def answering(reader=quoting_users, reviewer=None):
    def answer(role, data):
        if role == "reader":
            return reader(data)
        if role == "reviewer":
            return reviewer(data) if callable(reviewer) else (reviewer or EMPTY_REVIEW)
        raise AssertionError(f"unexpected {role} call")

    return answer


def dream(
    db,
    home,
    cfg,
    apsis,
    model,
    *,
    run_id="dream-test",
    selection=None,
    clock=None,
    phases=None,
    started_at=None,
):
    """Run a dream's Read and Review steps; research and author steps are local
    stand-ins."""
    fields = {"selection": selection or {}}
    if started_at:
        fields["started_at"] = started_at
    add_run(db, run_id, cfg, **fields)
    found = instructions(cfg)
    read = observe.ReadPhase(found, apsis)
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases=phases
        or {
            "read": read,
            "review": review.ReviewPhase(found),
            "research": LocalPhase(),
            "author": LocalPhase(),
        },
        model=model,
        clock=clock or FakeClock(),
        planner=read.plan,
    )
    return runner.execute()

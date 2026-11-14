"""`dream trial reader`: a backend's reader quality on recorded sessions, with no
store writes (5.6, design Risks and Assumptions)."""

import json

import pytest
from dreamkit import (
    INSTRUCTIONS,
    ScriptedModel,
    build_config,
    instructions,
    observation,
)
from runkit import dump
from sessiondocs import StubApsis, days_ago, make_doc, make_entry, select

from dreaming import cli, observe, sessions
from dreaming.backends import BackendError, probe


def doc(sid, text, days=2):
    when = days_ago(days)
    entries = [
        make_entry("user", text, when, id=f"{sid}-u"),
        make_entry("assistant", "All done, it works!", when, id=f"{sid}-a"),
    ]
    return make_doc("claude", sid, when, entries=entries)


def three_sessions():
    return StubApsis(
        doc("s1", "please use uv"), doc("s2", "please use uv", 3), doc("s3", "no", 4)
    )


def mixed_reader(data):
    """s1: a matching quote and an invented one; s2: output off the schema; s3:
    a user statement quoting the assistant."""
    user, assistant = data["entries"]
    if user["id"] == "s1-u":
        return {
            "observations": [
                observation(user, "use uv", "prefers uv"),
                observation(user, "never said this", "invented"),
            ]
        }
    if user["id"] == "s2-u":
        return {"notes": "free text"}
    return {
        "observations": [
            observation(assistant, "it works", "claims", kind="user_statement")
        ]
    }


def test_the_trial_reports_schema_valid_and_quote_match_rates_and_tokens(db, home):
    cfg = build_config()
    chosen = select(db, three_sessions(), cfg, home).sessions
    model = ScriptedModel(lambda role, data: mixed_reader(data))
    before = dump(db)
    result = observe.trial_reader(chosen, model, instructions(cfg), cfg)
    assert result["backend"] == "fake"
    assert (result["sessions"], result["calls"], result["failed_calls"]) == (3, 3, 0)
    assert (result["schema_valid"], result["schema_valid_rate"]) == (2, 0.667)
    # Three observations, two quoting their entry; one passes every check.
    assert (result["observations"], result["quote_matches"]) == (3, 2)
    assert result["quote_match_rate"] == 0.667
    assert result["accepted"] == 1
    assert (result["tokens_in"], result["tokens_out"]) == (300, 60)
    assert result["usage_estimated"] is False
    assert dump(db) == before


def test_a_failed_call_is_counted_once_and_not_retried(db, home):
    cfg = build_config()
    chosen = select(db, three_sessions(), cfg, home).sessions

    def answer(role, data):
        raise BackendError("connection refused")

    model = ScriptedModel(answer)
    result = observe.trial_reader(chosen, model, instructions(cfg), cfg)
    assert (result["calls"], result["failed_calls"]) == (3, 3)
    assert result["schema_valid_rate"] is None
    assert result["errors"] == ["connection refused"] * 3
    assert len(model.inputs) == 3


@pytest.fixture
def trial_model(home, monkeypatch):
    """A configured backend `local`, a stub apsis and a scripted model in place of
    the probed adapter."""
    (home / "config.toml").write_text(
        '[backends.local]\nharness = "lmstudio"\ncontext_tokens = 100000\n'
    )
    model = ScriptedModel(lambda role, data: mixed_reader(data))
    model.name = "local"
    probed = probe.ProbeResult(
        "local",
        "lmstudio",
        "1.0",
        status="cached",
        agent_enforcement="removed",
        apsis_finds_session=True,
    )
    monkeypatch.setattr(probe, "cached_status", lambda home, cfg, name: probed)
    monkeypatch.setattr(probe, "adapter", lambda home, cfg, run_id, result: model)
    monkeypatch.setattr(
        sessions.Apsis,
        "from_config",
        classmethod(lambda cls, cfg, home: three_sessions()),
    )
    return model


def test_dream_trial_reader_reads_processed_sessions_and_stores_nothing(
    db, home, trial_model, capsys
):
    for session in select(db, three_sessions(), build_config(), home).sessions:
        sessions.record_session(db, None, session)
    before = dump(db)
    args = ["trial", "reader", "--backend", "local", "--sessions", "2"]
    args += ["--instructions", str(INSTRUCTIONS)]
    assert cli.main([*args, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend"] == "local"
    # N recorded sessions, newest first, whether or not a dream read them.
    assert result["sessions"] == 2
    assert [d["entries"][0]["id"] for d in trial_model.of("reader")] == [
        "s1-u",
        "s2-u",
    ]
    assert dump(db) == before

    assert cli.main(args) == 0
    text = capsys.readouterr().out
    assert "Reader trial on local: 2 sessions, 2 calls, 0 failed" in text
    assert "quotes matching their entry: 1 of 2 (50%)" in text


def test_dream_trial_reader_refuses_an_unknown_backend(home, capsys):
    assert cli.main(["trial", "reader", "--backend", "nope"]) == 2
    assert "unknown backend 'nope'" in capsys.readouterr().err

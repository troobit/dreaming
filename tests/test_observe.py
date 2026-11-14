import json
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_selection import LENS, Source, document, select

from dreaming.config import load_config
from dreaming.observe import apply_observations, verify_observations, verify_quote
from dreaming.redact import redact
from dreaming.sessions import ContentBlock, Entry, register_session
from dreaming.store import Store


def observation(**changes):
    return dict(
        source="claude",
        session_id="s",
        entry_id="entry:0",
        source_location="entry:0",
        representation_version=1,
        quote="Prefer Make",
        kind="preference",
        modality="asserted",
        outcome="unknown",
        outcome_evidence=[],
        lens_id="preferences",
        lens_version="1",
        pattern_key="preferences: Make",
        subject="user",
        **changes,
    )


def selected_session(store, config, text="Prefer Make", role="user", **kwargs):
    doc = document("s", count=1, **kwargs)
    doc = replace(doc, entries=(Entry(role, (ContentBlock("text", {"text": text}),)),))
    return select(Source(doc), (config, store)).sessions[0]


@pytest.fixture
def env(tmp_path):
    with Store(tmp_path / "home") as store:
        config = load_config(tmp_path / "config.toml")
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        yield store, config


@given(st.text(min_size=1).filter(lambda s: bool(s.strip())), st.data())
def test_quote_verification_before_redaction_and_idempotence(text, data):
    start = data.draw(st.integers(0, len(text) - 1))
    end = data.draw(st.integers(start + 1, len(text)))
    quote = text[start:end]
    if quote.strip():
        assert verify_quote(text, quote)
    assert redact(redact(text)) == redact(text)


def verify(env, selected, observations):
    store, config = env
    return verify_observations(store, config, selected, observations, backend="fake", model="m")


def test_verified_redacted_evidence_is_idempotent_and_attributed(env):
    store, config = env
    secret = "sk-" + "a" * 30
    selected = selected_session(store, config, "Prefer Make " + secret)
    raw = observation()
    raw["quote"] += " " + secret
    accepted = verify(env, selected, [raw])
    assert secret not in json.dumps(accepted)
    with store.transaction() as conn:
        apply_observations(store, conn, "r", selected, config, accepted)
    with store.transaction() as conn:
        apply_observations(store, conn, "r", selected, config, accepted)
    row = store.connection.execute(
        "SELECT kind,modality,outcome,backend,model,lens_version,source_location FROM observations"
    ).fetchall()
    assert row == [("user_statement", "asserted", "unknown", "fake", "m", LENS.version, "entry:0")]
    assert (
        json.loads(store.connection.execute("SELECT statistics FROM runs").fetchone()[0])[
            "observations_accepted"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("quote", "absent"),
        ("source_location", "entry:99"),
        ("entry_id", "wrong"),
        ("lens_version", "unknown"),
        ("representation_version", True),
    ],
)
def test_wrong_location_quote_and_lens_are_rejected_and_counted(env, field, value):
    store, config = env
    selected = selected_session(store, config)
    raw = observation()
    raw[field] = value
    result = verify(env, selected, [raw])
    assert not result["accepted"] and len(result["rejected"]) == 1
    with store.transaction() as conn:
        apply_observations(store, conn, "r", selected, config, result)
    assert (
        json.loads(store.connection.execute("SELECT statistics FROM runs").fetchone()[0])[
            "observations_rejected"
        ]
        == 1
    )


def test_tool_prompts_and_supplied_context_cannot_be_user_evidence(env):
    store, config = env
    selected = selected_session(store, config)
    register_session(store, source="claude", session_id="s", tool="orbit", run_ref="job")
    assert not verify(env, selected, [observation()])["accepted"]
    register_session(
        store,
        source="claude",
        session_id="s",
        tool="orbit",
        run_ref="quoted",
        user_quote_entries=("entry:0",),
    )
    assert verify(env, selected, [observation()])["accepted"]
    supplied = replace(
        selected,
        document=replace(
            selected.document,
            entries=(replace(selected.document.entries[0], supplied_context=True),),
        ),
    )
    assert not verify(env, supplied, [observation()])["accepted"]


def test_assistant_success_never_confirms_an_outcome(env):
    store, config = env
    selected = selected_session(store, config, "Everything passed", role="assistant")
    raw = observation()
    raw.update(
        quote="Everything passed", kind="approach", outcome="success", outcome_evidence=["entry:0"]
    )
    assert verify(env, selected, [raw])["accepted"][0]["outcome"] == "unknown"


def test_verified_check_outcome_and_hedge_are_preserved(env):
    store, config = env
    selected = selected_session(store, config, "Maybe use Make")
    selected = replace(
        selected,
        document=replace(
            selected.document,
            entries=(
                *selected.document.entries,
                Entry(
                    "tool", (ContentBlock("tool_result", {"text": "tests passed", "exit_code": 0}),)
                ),
            ),
        ),
    )
    raw = observation()
    raw.update(
        quote="Maybe use Make",
        kind="approach",
        modality="hedged",
        outcome="success",
        outcome_evidence=["entry:1"],
    )
    accepted = verify(env, selected, [raw])["accepted"][0]
    assert accepted["outcome"] == "check-confirmed" and accepted["modality"] == "hedged"


def test_apsis_tool_result_content_in_a_user_envelope_is_not_a_user_statement(env):
    store, config = env
    selected = selected_session(store, config)
    selected = replace(
        selected,
        document=replace(
            selected.document,
            entries=(
                Entry(
                    "user",
                    (
                        ContentBlock(
                            "tool_result",
                            {
                                "type": "tool_result",
                                "content": "Tests passed",
                                "is_error": False,
                                "exit_code": 0,
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    raw = observation()
    raw.update(
        quote="Tests passed", kind="approach", outcome="success", outcome_evidence=["entry:0"]
    )
    accepted = verify(env, selected, [raw])["accepted"][0]
    assert accepted["kind"] == "tool_result" and accepted["outcome"] == "check-confirmed"
    raw["kind"] = "user_statement"
    assert not verify(env, selected, [raw])["accepted"]


def test_checkpoint_survives_source_removal_and_preserves_availability(env):
    store, config = env
    selected = selected_session(store, config)
    checkpoint = verify(env, selected, [observation()])
    unavailable = replace(selected, document=replace(selected.document, entries=()))
    with store.transaction() as conn:
        apply_observations(store, conn, "r", unavailable, config, checkpoint)
    assert store.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert (
        store.connection.execute("SELECT availability FROM observations").fetchone()[0]
        == "unavailable"
    )

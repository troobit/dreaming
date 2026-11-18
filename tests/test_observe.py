"""Observation verification, redaction and the attribution gate (2.9, 6.1-6.3,
13.1)."""

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from sessiondocs import StubApsis, add_run, days_ago, make_doc, make_entry, select

from dreaming import config, observe
from dreaming.errors import Rejected
from dreaming.redact import REDACTED, redact

SECRETS = [
    "Bearer abcdef0123456789.xyz",
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_" + "a" * 36,
    "xoxb-1234-abcd",
    "API_KEY=hunter2hunter2",
    "password: correct-horse",
    "postgres://admin:s3cret@db.example.com",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "sk-ant-" + "b" * 32,
    "github_pat_" + "c" * 30,
    "AIza" + "d" * 35,
    "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4",
]
LENSES = {"preferences": 1, "successful-approaches": 2}


@pytest.fixture
def cfg():
    return config.build_config({"domains": {"projects": {"/work": "software"}}})


def tool_result(text, entry_id):
    return {
        "role": "user",
        "id": entry_id,
        "content": [{"type": "tool_result", "content": text}],
    }


def one_session(db, home, cfg, entries, *, origin=None):
    doc = make_doc(
        "claude",
        "s1",
        days_ago(2),
        entries=entries,
        project="/work/repo",
        origin=origin,
    )
    (session,) = select(db, StubApsis(doc), cfg, home).sessions
    return session


def raw(entry_id, quote, **fields):
    return {
        "entry_id": entry_id,
        "quote": quote,
        "kind": "user_statement",
        "modality": "asserted",
        "outcome": "unknown",
        "outcome_entry_id": None,
        "lens": "preferences",
        "pattern_key": "prefers uv over pip",
        **fields,
    }


def entry(location, text, role="user", check=False):
    return {location: observe.SourceEntry(location, role, text, check, "2026-11-12")}


# Verification and redaction (6.2)

drawn = st.text(min_size=1).flatmap(
    lambda text: st.tuples(
        st.just(text),
        st.integers(0, len(text)),
        st.integers(0, len(text)),
    )
)


@given(drawn)
def test_a_quote_drawn_from_its_entry_always_verifies(case):
    text, start, end = case
    quote = text[min(start, end) : max(start, end)]
    assume(quote.strip())
    assert observe.verify(quote, text)
    # Whitespace differs between the reader's copy and the source.
    assert observe.verify("\n  ".join(quote.split()), text)


@given(st.lists(st.one_of(st.text(), st.sampled_from(SECRETS))).map("".join))
def test_redaction_is_idempotent(text):
    once = redact(text)
    assert redact(once) == once


@pytest.mark.parametrize("secret", SECRETS)
def test_redaction_removes_credentials_and_tokens(secret):
    redacted = redact(f"ran it with {secret} and it worked")
    assert secret not in redacted
    assert REDACTED in redacted
    assert redacted.startswith("ran it with ")


def test_text_without_credentials_is_kept():
    text = "please use uv, never pip; the key point is speed"
    assert redact(text) == text


def test_a_quote_is_verified_against_the_source_and_stored_redacted(db, home, cfg):
    secret = "sk-ant-" + "z" * 32
    session = one_session(
        db,
        home,
        cfg,
        [make_entry("user", f"Run it with ANTHROPIC_API_KEY={secret} set", id="u1")],
    )
    entries = observe.source_entries(session)
    obs = observe.gate(raw("u1", f"ANTHROPIC_API_KEY={secret} set"), entries, LENSES)
    observe.store(db, None, session, obs, backend="fake", model="m")
    (quote,) = db.execute("SELECT quote FROM observations").fetchone()
    assert secret not in quote
    assert REDACTED in quote


# The gate (2.9, 6.2, 13.1)


def test_a_quote_not_in_the_entry_it_cites_is_rejected():
    entries = {
        **entry("u1", "please use uv, never pip"),
        **entry("u2", "thanks"),
    }
    with pytest.raises(Rejected, match="does not match entry u2"):
        observe.gate(raw("u2", "please use uv"), entries, LENSES)


def test_an_unknown_entry_or_lens_is_rejected():
    entries = entry("u1", "please use uv")
    with pytest.raises(Rejected, match="not in the part read"):
        observe.gate(raw("u9", "please use uv"), entries, LENSES)
    with pytest.raises(Rejected, match="lens skill-use was not asked for"):
        observe.gate(raw("u1", "please use uv", lens="skill-use"), entries, LENSES)


def test_a_user_statement_on_a_tool_attributed_entry_is_rejected(db, home, cfg):
    session = one_session(
        db,
        home,
        cfg,
        [make_entry("user", "Implement task 3 using uv only", id="u1")],
        origin="sdk-cli",
    )
    entries = observe.source_entries(session)
    assert entries["u1"].role == "tool"
    with pytest.raises(Rejected, match="attributed to the tool"):
        observe.gate(raw("u1", "using uv only"), entries, LENSES)
    kept = observe.gate(raw("u1", "using uv only", kind="tool_prompt"), entries, LENSES)
    assert kept.kind == "tool_prompt"


def test_a_user_statement_must_quote_the_user():
    entries = entry("a1", "You should use uv", role="assistant")
    with pytest.raises(Rejected, match="attributed to the assistant"):
        observe.gate(raw("a1", "use uv"), entries, LENSES)


def test_modality_and_outcome_labels_are_kept(db, home, cfg):
    session = one_session(
        db,
        home,
        cfg,
        [
            make_entry("user", "I think make test first is better", id="u1"),
            tool_result("12 passed", "t1"),
        ],
    )
    obs = observe.gate(
        raw(
            "u1",
            "I think make test first",
            modality="hedged",
            outcome="check_confirmed",
            outcome_entry_id="t1",
            lens="successful-approaches",
        ),
        observe.source_entries(session),
        LENSES,
    )
    observe.store(db, None, session, obs, backend="fake", model="m")
    row = db.execute("SELECT * FROM observations").fetchone()
    assert (row["modality"], row["outcome"], row["outcome_evidence"]) == (
        "hedged",
        "check_confirmed",
        "t1",
    )
    assert row["lens_version"] == 2


@pytest.mark.parametrize(
    "outcome, shown_by, kept",
    [
        ("user_confirmed", "a1", "unknown"),  # the assistant's claim
        ("check_confirmed", "a1", "unknown"),
        ("failed", "a1", "unknown"),
        ("user_confirmed", "u2", "user_confirmed"),
        ("check_confirmed", "t1", "check_confirmed"),
        ("check_confirmed", "u2", "unknown"),  # the user's words are not a check
        ("failed", "t1", "failed"),
        ("user_confirmed", None, "unknown"),
        ("check_confirmed", "x9", "unknown"),
    ],
)
def test_an_assistant_claim_of_success_never_becomes_a_confirmed_outcome(
    outcome, shown_by, kept
):
    entries = {
        **entry("u1", "try make test before committing"),
        **entry("a1", "Done! All tests pass now.", role="assistant"),
        **entry("t1", "[tool_result] 12 passed", role="tool", check=True),
        **entry("u2", "that worked, thanks"),
    }
    obs = observe.gate(
        raw(
            "u1",
            "make test before committing",
            outcome=outcome,
            outcome_entry_id=shown_by,
        ),
        entries,
        LENSES,
    )
    assert obs.outcome == kept
    assert obs.outcome_entry == (shown_by if kept != "unknown" else None)


def test_pattern_keys_are_normalised():
    entries = entry("u1", "please use uv")
    obs = observe.gate(
        raw("u1", "use uv", pattern_key="  Preferences:  Prefers UV over  pip "),
        entries,
        LENSES,
    )
    assert obs.pattern_key == "prefers uv over pip"
    with pytest.raises(Rejected, match="no pattern key"):
        observe.gate(raw("u1", "use uv", pattern_key=" "), entries, LENSES)


# Storing (6.1, 6.3)


def test_an_observation_records_its_evidence_fields(db, home, cfg):
    session = one_session(
        db, home, cfg, [make_entry("user", "please use uv", days_ago(2), id="u1")]
    )
    obs = observe.gate(raw("u1", "use uv"), observe.source_entries(session), LENSES)
    add_run(db, "dream-1")
    evidence_id = observe.store(db, "dream-1", session, obs, backend="b", model="m")
    row = db.execute("SELECT * FROM observations").fetchone()
    assert row["evidence_id"] == evidence_id
    assert (row["source"], row["session_id"], row["harness"]) == (
        "claude",
        "s1",
        "claude",
    )
    assert (row["entry_id"], row["location"], row["representation_version"]) == (
        "u1",
        "u1",
        1,
    )
    assert (row["lens"], row["lens_version"]) == ("preferences", 1)
    assert (row["backend"], row["model"], row["run"]) == ("b", "m", "dream-1")
    assert (row["project"], row["domain"], row["subject"]) == (
        "/work/repo",
        "software",
        "user",
    )
    assert row["observed_on"] == days_ago(2)[:10]
    assert row["pattern_key"] == "prefers uv over pip"


def test_storing_the_same_observation_again_adds_nothing(db, home, cfg):
    session = one_session(db, home, cfg, [make_entry("user", "please use uv", id="u1")])
    entries = observe.source_entries(session)
    for run_id in ("r1", "r2", "r3"):
        add_run(db, run_id)
    first = observe.store(
        db,
        "r1",
        session,
        observe.gate(raw("u1", "use uv"), entries, LENSES),
        backend="b",
        model="m",
    )
    again = observe.store(
        db,
        "r2",
        session,
        observe.gate(raw("u1", "use  uv"), entries, LENSES),
        backend="other",
        model="n",
    )
    assert first == again
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    # A new lens version is a new observation.
    observe.store(
        db,
        "r3",
        session,
        observe.gate(raw("u1", "use uv"), entries, {"preferences": 2}),
        backend="b",
        model="m",
    )
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_a_rejected_observation_keeps_only_a_redacted_short_quote():
    detail = observe.rejection_detail(
        raw("u1", "token=abcdef " + "x" * 400), quote_chars=50
    )
    assert "abcdef" not in detail["quote"]
    assert len(detail["quote"]) <= 50

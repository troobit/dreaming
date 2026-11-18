"""Forgetting and restoring a session, and the counts it changes (2.11, 4.5, 13.3)."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sessiondocs import (
    NOW,
    StubApsis,
    add_item,
    add_observation,
    add_pattern,
    add_seen,
    fixture,
    iso,
    pending,
    select,
)

from dreaming import config, knowledge, sessions, store
from dreaming.errors import DreamingError

TODAY = date(2026, 11, 14)
SERVED = "0199cccc-0000-7000-8000-000000000003"
LMSTUDIO_ID = "lmstudio-thread-130d11ea3c18a18b3e2af1056ebe36cdab4e4715e0db09df"


@pytest.fixture
def cfg():
    return config.build_config({"thresholds": {"min_sessions": 3}})


def _forgotten(db):
    rows = db.execute("SELECT source, session_id, forgotten FROM sessions_seen")
    return {(row["source"], row["session_id"]): row["forgotten"] for row in rows}


def _two_copies(db):
    """An interaction read through Codex and LM Studio, plus an unrelated session."""
    add_seen(db, "codex", "c1", thread_id="t1")
    add_seen(db, "lmstudio", "l1", thread_id="t1")
    add_seen(db, "claude", "other")
    add_observation(db, "o1", "codex", "c1")
    add_observation(db, "o2", "claude", "other")
    add_item(db, "k1", ["o1", "o2"])
    add_item(db, "k2", ["o2"])


def test_forgetting_an_unknown_session_names_it(db):
    with pytest.raises(DreamingError) as excinfo:
        knowledge.forget_session(db, "nope")
    assert excinfo.value.code == "not_found"
    assert "nope" in excinfo.value.message
    with pytest.raises(DreamingError):
        knowledge.restore_session(db, "nope")


def test_forgetting_marks_every_copy_and_queues_the_knowledge_it_supported(db):
    _two_copies(db)
    result = knowledge.forget_session(db, "c1")
    assert result["changed"]
    assert result["state"] == "forgotten"
    assert sorted(result["copies"]) == [["codex", "c1"], ["lmstudio", "l1"]]
    assert result["reassess"] == ["k1"]
    assert _forgotten(db) == {
        ("codex", "c1"): 1,
        ("lmstudio", "l1"): 1,
        ("claude", "other"): 0,
    }
    assert pending(db) == {"k1": 1, "k2": 0}
    # Forgetting keeps the stored data.
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2


def test_a_session_can_be_forgotten_through_any_copy(db):
    _two_copies(db)
    knowledge.forget_session(db, "l1", source="lmstudio")
    assert _forgotten(db)[("codex", "c1")] == 1


def test_repeating_forget_or_restoring_an_active_session_changes_nothing(db):
    _two_copies(db)
    knowledge.forget_session(db, "c1")
    before = db.total_changes
    assert not knowledge.forget_session(db, "c1")["changed"]
    assert db.total_changes == before
    knowledge.restore_session(db, "c1")
    before = db.total_changes
    assert not knowledge.restore_session(db, "c1")["changed"]
    assert db.total_changes == before


def test_restoring_counts_the_session_again_and_queues_knowledge_again(db):
    _two_copies(db)
    knowledge.forget_session(db, "c1")
    db.execute("UPDATE knowledge_items SET pending_reassessment = 0")
    result = knowledge.restore_session(db, "c1")
    assert result["changed"]
    assert result["state"] == "active"
    assert result["reassess"] == ["k1"]
    assert pending(db) == {"k1": 1, "k2": 0}
    assert set(_forgotten(db).values()) == {0}


def test_forgotten_sessions_are_not_selected_even_on_request(db, home):
    cfg = config.build_config({})
    apsis = StubApsis(fixture("session-codex-lmstudio"), fixture("session-lmstudio"))
    add_seen(db, "codex", SERVED, thread_id=SERVED)
    add_seen(db, "lmstudio", LMSTUDIO_ID, thread_id=SERVED)
    knowledge.forget_session(db, LMSTUDIO_ID)
    reread = sessions.SelectionRequest(reread=True)
    assert select(db, apsis, cfg, home).sessions == []
    assert select(db, apsis, cfg, home, reread).sessions == []
    knowledge.restore_session(db, SERVED)
    assert [s.id for s in select(db, apsis, cfg, home, reread).sessions] == [SERVED]


def test_registration_rejects_a_forgotten_session(db):
    _two_copies(db)
    knowledge.forget_session(db, "c1")
    results = sessions.register_sessions(
        db,
        "orbit",
        "run-1",
        [
            {"source": "codex", "session_id": "c1"},
            {"source": "codex-exec", "session_id": "x", "correlation_id": "t1"},
        ],
        now=NOW,
    )
    assert [result["state"] for result in results] == ["forgotten", "forgotten"]
    assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


def test_forgotten_and_excluded_observations_are_not_counted_or_given_to_roles(db):
    _two_copies(db)
    add_seen(db, "claude", "third")
    add_observation(db, "o3", "claude", "third")
    db.execute("UPDATE observations SET excluded = 1 WHERE evidence_id = 'o3'")
    knowledge.forget_session(db, "c1")
    counted = [row["evidence_id"] for row in knowledge.counted_observations(db)]
    assert counted == ["o2"]


# Support withheld at once (2.11, 4.5, 13.3)


def _sessions_with_observations(db, count, **observation):
    for n in range(1, count + 1):
        add_seen(db, "claude", f"s{n}", thread_id=f"t{n}")
        add_observation(db, f"o{n}", "claude", f"s{n}", kind="assistant_suggestion")
    for evidence_id, values in observation.items():
        assignments = ", ".join(f"{column} = ?" for column in values)
        db.execute(
            f"UPDATE observations SET {assignments} WHERE evidence_id = ?",
            (*values.values(), evidence_id),
        )


def test_an_item_left_below_min_sessions_is_withheld_at_once(db, cfg):
    _sessions_with_observations(db, 3)
    add_item(db, "k1", ["o1", "o2", "o3"])
    assert not knowledge.item_withheld(db, "k1", cfg)
    knowledge.forget_session(db, "s1")
    assert knowledge.item_withheld(db, "k1", cfg)
    knowledge.restore_session(db, "s1")
    assert not knowledge.item_withheld(db, "k1", cfg)


def test_an_item_resting_on_an_explicit_user_statement_needs_that_statement(db, cfg):
    _sessions_with_observations(db, 2, o1={"kind": "user_statement"})
    add_item(db, "explicit", ["o1"])
    add_item(db, "hedged", ["o2"])
    db.execute(
        "UPDATE observations SET kind = 'user_statement', modality = 'hedged'"
        " WHERE evidence_id = 'o2'"
    )
    assert not knowledge.item_withheld(db, "explicit", cfg)
    assert knowledge.item_withheld(db, "hedged", cfg)
    knowledge.forget_session(db, "s1")
    assert knowledge.item_withheld(db, "explicit", cfg)


def test_a_claim_that_an_approach_worked_needs_a_confirmed_success(db, cfg):
    _sessions_with_observations(
        db, 4, o1={"outcome": "check_confirmed"}, o2={"outcome": "failed"}
    )
    add_item(db, "k1", ["o1", "o2", "o3", "o4"], kind="workflow")
    assert not knowledge.item_withheld(db, "k1", cfg)
    knowledge.forget_session(db, "s1")
    # Three sessions remain, but only a failed outcome: not enough (13.3).
    assert knowledge.item_withheld(db, "k1", cfg)


def test_a_change_on_forgotten_evidence_is_withheld_but_file_findings_stand(db, cfg):
    _sessions_with_observations(db, 3)
    db.execute(
        "INSERT INTO file_findings (evidence_id, repo, revision, path, location,"
        " finding, lens, lens_version) VALUES"
        " ('f1', 'repo', 'abc', 'SKILL.md', '12', 'broken link', 'refs', 1)"
    )
    for change_id, evidence in (("c-sessions", ["o1", "o2", "o3"]), ("c-file", ["f1"])):
        db.execute(
            "INSERT INTO changes (id, repo, evidence, status, created_at)"
            " VALUES (?, 'repo', ?, 'proposed', ?)",
            (change_id, json.dumps(evidence), iso(NOW)),
        )
    knowledge.forget_session(db, "s1")
    assert knowledge.change_withheld(db, "c-sessions", cfg)
    assert not knowledge.change_withheld(db, "c-file", cfg)


# Properties: counts and rankings follow the non-forgotten sessions


def _expected(observations, forgotten):
    found = {}
    for session, pattern, project, age in observations:
        if session in forgotten:
            continue
        found.setdefault(f"p{pattern}", []).append((session, project, age))
    return {
        pattern: knowledge.Signals(
            observations=len(rows),
            sessions=len({session for session, _, _ in rows}),
            projects=len({project for _, project, _ in rows if project}),
            last_date=(
                TODAY - timedelta(days=min(age for *_, age in rows))
            ).isoformat(),
        )
        for pattern, rows in found.items()
    }


OBSERVATIONS = st.lists(
    st.tuples(
        st.integers(0, 4),
        st.integers(0, 2),
        st.sampled_from(["/a", "/b", None]),
        st.integers(0, 90),
    ),
    min_size=1,
    max_size=25,
)
OPERATIONS = st.lists(
    st.tuples(st.sampled_from(["forget", "restore"]), st.integers(0, 4)), max_size=12
)


@settings(max_examples=60, deadline=None)
@given(observations=OBSERVATIONS, operations=OPERATIONS)
def test_counts_and_rankings_equal_the_non_forgotten_computation(
    observations, operations
):
    cfg = config.build_config({})
    conn = store.open_store(Path(":memory:"))
    for session in range(5):
        add_seen(conn, "claude", f"s{session}", thread_id=f"t{session}")
    add_seen(conn, "lmstudio", "l0", thread_id="t0")
    for pattern in range(3):
        add_pattern(conn, f"p{pattern}")
    for n, (session, pattern, project, age) in enumerate(observations):
        add_observation(
            conn,
            f"o{n}",
            "claude",
            f"s{session}",
            pattern_id=f"p{pattern}",
            project=project,
            observed_on=(TODAY - timedelta(days=age)).isoformat(),
        )
    original = knowledge.pattern_signals(conn)
    assert original == _expected(observations, set())

    forgotten: set[int] = set()
    for operation, session in operations:
        if operation == "forget":
            knowledge.forget_session(conn, f"s{session}")
            forgotten.add(session)
        else:
            knowledge.restore_session(conn, f"s{session}")
            forgotten.discard(session)
        expected = _expected(observations, forgotten)
        signals = knowledge.pattern_signals(conn)
        assert signals == expected
        assert knowledge.rank_patterns(signals, cfg, TODAY) == knowledge.rank_patterns(
            expected, cfg, TODAY
        )

    for session in range(5):
        knowledge.restore_session(conn, f"s{session}")
    assert knowledge.pattern_signals(conn) == original
    conn.close()


def test_ranking_orders_by_score_then_id(cfg):
    signals = {
        "p-b": knowledge.Signals(
            observations=3, sessions=3, projects=1, last_date="2026-11-14"
        ),
        "p-a": knowledge.Signals(
            observations=3, sessions=3, projects=1, last_date="2026-11-14"
        ),
        "p-old": knowledge.Signals(
            observations=9, sessions=3, projects=1, last_date="2025-01-01"
        ),
        "p-wide": knowledge.Signals(
            observations=4, sessions=4, projects=2, last_date="2026-11-01"
        ),
    }
    ranked = knowledge.rank_patterns(signals, cfg, TODAY)
    assert [pattern for pattern, _ in ranked] == ["p-wide", "p-a", "p-b", "p-old"]
    weights = cfg["ranking"]
    assert ranked[1][1] == pytest.approx(
        weights["w_sessions"] * 3 + weights["w_recency"] * 1 + weights["w_projects"] * 1
    )

"""Session selection, de-duplication and splitting (1.3-1.5, 2.4-2.8)."""

import json
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sessiondocs import (
    NOW,
    StubApsis,
    add_run,
    add_seen,
    days_ago,
    fixture,
    iso,
    make_doc,
    make_entry,
    select,
    selected_ids,
)

from dreaming import config, roles, sessions
from dreaming.config import Lens
from dreaming.errors import DreamingError


@pytest.fixture
def cfg():
    return config.build_config({"budget": {"sessions": 10}})


# Default selection (1.3, 2.5)


def test_unread_sessions_are_selected_newest_first_within_the_session_budget(db, home):
    apsis = StubApsis(
        make_doc("claude", "old", days_ago(9)),
        make_doc("claude", "newest", days_ago(1)),
        make_doc("codex", "middle", days_ago(3)),
        make_doc("copilot", "older", days_ago(5)),
    )
    cfg = config.build_config({"budget": {"sessions": 3}})
    selection = select(db, apsis, cfg, home)
    assert selected_ids(selection) == ["newest", "middle", "older"]


def test_a_processed_session_contributes_only_its_unread_additions(db, home, cfg):
    entries = [
        make_entry("user", f"line {i}", days_ago(2), id=f"e{i}") for i in range(4)
    ]
    apsis = StubApsis(
        make_doc("claude", "grown", days_ago(2), entries=entries),
        make_doc("claude", "unchanged", days_ago(3)),
    )
    add_seen(db, "claude", "grown", entries_read=2)
    add_seen(db, "claude", "unchanged", entries_read=2)
    selection = select(db, apsis, cfg, home)
    (grown,) = selection.sessions
    assert grown.id == "grown"
    assert grown.start == 2
    assert [e.location for e in grown.entries()] == ["e2", "e3"]


def test_a_processed_session_with_an_unchanged_listing_is_not_opened(db, home, cfg):
    same = make_doc("claude", "same", days_ago(2))
    grown = make_doc("claude", "grown", days_ago(3))
    same["session"]["size_bytes"] = 100
    grown["session"]["size_bytes"] = 300
    apsis = StubApsis(same, grown)
    for sid in ("same", "grown"):
        add_seen(db, "claude", sid, entries_read=1)
    db.execute("UPDATE sessions_seen SET size_bytes = 100")
    selection = select(db, apsis, cfg, home)
    assert selected_ids(selection) == ["grown"]
    assert ("claude", "same") not in apsis.opened


def test_failed_sessions_are_not_retried_by_default(db, home, cfg):
    apsis = StubApsis(make_doc("claude", "failed", days_ago(2)))
    add_seen(db, "claude", "failed", state="failed", entries_read=0)
    assert selected_ids(select(db, apsis, cfg, home)) == []


def test_skipped_sessions_are_tried_again(db, home, cfg):
    apsis = StubApsis(make_doc("claude", "skipped", days_ago(2)))
    add_seen(db, "claude", "skipped", state="skipped", entries_read=0)
    assert selected_ids(select(db, apsis, cfg, home)) == ["skipped"]


# Explicit selection (1.4)


@pytest.mark.parametrize(
    "request_",
    [
        sessions.SelectionRequest(reread=True),
        sessions.SelectionRequest(since=NOW - timedelta(days=4)),
        sessions.SelectionRequest(project="/work/repo"),
    ],
)
def test_explicit_selection_includes_processed_sessions_from_the_start(
    db, home, cfg, request_
):
    apsis = StubApsis(make_doc("claude", "done", days_ago(2)))
    add_seen(db, "claude", "done", entries_read=2)
    add_seen(db, "claude", "failed", state="failed", entries_read=0)
    apsis.docs[("claude", "failed")] = make_doc("claude", "failed", days_ago(3))
    selection = select(db, apsis, cfg, home, request_)
    assert selected_ids(selection) == ["done", "failed"]
    assert all(s.start == 0 for s in selection.sessions)


def test_date_range_selects_sessions_started_inside_it(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "before", days_ago(10)),
        make_doc("claude", "inside", days_ago(5)),
        make_doc("claude", "after", days_ago(1)),
    )
    request = sessions.SelectionRequest(
        since=NOW - timedelta(days=7), until=NOW - timedelta(days=2)
    )
    assert selected_ids(select(db, apsis, cfg, home, request)) == ["inside"]


def test_project_filter_matches_the_project_and_paths_under_it(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "repo", days_ago(1), project="/work/repo"),
        make_doc("claude", "sub", days_ago(2), project="/work/repo/sub"),
        make_doc("claude", "other", days_ago(3), project="/work/other"),
        make_doc("claude", "none", days_ago(4), project=None),
    )
    request = sessions.SelectionRequest(project="/work/repo")
    assert selected_ids(select(db, apsis, cfg, home, request)) == ["repo", "sub"]


# The dream in progress and earlier dreams (1.5)


def test_sessions_of_the_running_dream_are_excluded(db, home, cfg):
    run_id = "dream-20261114-current"
    add_run(db, run_id)
    db.execute(
        "INSERT INTO calls (run, attempt, role, backend, session_ref, started_at)"
        " VALUES (?, 1, 'reader', 'claude', 'call-session', ?)",
        (run_id, iso(NOW)),
    )
    apsis = StubApsis(
        make_doc("claude", "call-session", days_ago(1), project="/elsewhere"),
        make_doc("lmstudio", "lms-1", days_ago(1), thread_id=run_id, project=None),
        make_doc(
            "claude", "cwd", days_ago(1), project=str(home / "runs" / run_id / "w")
        ),
        make_doc("claude", "user", days_ago(1)),
    )
    selection = select(db, apsis, cfg, home, run_id=run_id)
    assert selected_ids(selection) == ["user"]


def test_sessions_of_earlier_dreams_are_selected_as_subject_dreaming(db, home, cfg):
    add_run(db, "dream-earlier")
    add_run(db, "dream-current")
    db.execute(
        "INSERT INTO calls (run, attempt, role, backend, harness_session_id,"
        " started_at) VALUES ('dream-earlier', 1, 'reader', 'claude', 'old-call', ?)",
        (iso(NOW),),
    )
    apsis = StubApsis(
        make_doc("claude", "old-call", days_ago(2), project="/elsewhere"),
        make_doc("claude", "probe", days_ago(2), project=str(home / "probes" / "c")),
        make_doc(
            "lmstudio", "lms", days_ago(2), thread_id="dream-probe-ab", project=None
        ),
        make_doc("claude", "user", days_ago(3)),
    )
    selection = select(db, apsis, cfg, home, run_id="dream-current")
    subjects = {s.id: s.attribution.subject for s in selection.sessions}
    assert subjects == {
        "old-call": "dreaming",
        "probe": "dreaming",
        "lms": "dreaming",
        "user": "user",
    }


@pytest.mark.parametrize(
    "meta",
    [
        {"created_at": iso(NOW - timedelta(minutes=10))},
        {"file_modified_at": iso(NOW - timedelta(minutes=10)), "created_at": None},
    ],
)
def test_recently_modified_sessions_are_excluded(db, home, cfg, meta):
    recent = make_doc("claude", "recent", days_ago(1))
    recent["session"].update(meta)
    recent["session"] = {k: v for k, v in recent["session"].items() if v}
    apsis = StubApsis(recent, make_doc("claude", "settled", days_ago(2)))
    assert selected_ids(select(db, apsis, cfg, home)) == ["settled"]


def test_a_session_with_a_recent_entry_is_excluded(db, home, cfg):
    entries = [
        make_entry("user", "start", days_ago(1), id="u1"),
        make_entry(
            "assistant", "still going", iso(NOW - timedelta(minutes=5)), id="a1"
        ),
    ]
    apsis = StubApsis(make_doc("claude", "active", days_ago(1), entries=entries))
    assert selected_ids(select(db, apsis, cfg, home)) == []


# One interaction across sources (2.4)


def _matched_apsis():
    return StubApsis(fixture("session-codex-lmstudio"), fixture("session-lmstudio"))


def test_an_interaction_in_two_sources_is_selected_once(db, home, cfg):
    selection = select(db, _matched_apsis(), cfg, home)
    (session,) = selection.sessions
    assert (session.source, session.id) == (
        "codex",
        "0199cccc-0000-7000-8000-000000000003",
    )
    assert {(row.source, row.id) for row in session.copies} == {
        ("codex", "0199cccc-0000-7000-8000-000000000003"),
        (
            "lmstudio",
            "lmstudio-thread-130d11ea3c18a18b3e2af1056ebe36cdab4e4715e0db09df",
        ),
    }
    # Content from the harness, server metadata from the log.
    assert session.document.source == "codex"
    assert [r["endpoint"] for r in session.server_requests] == ["/v1/responses"] * 4


def test_the_log_supplies_content_when_it_holds_the_only_copy(db, home, cfg):
    doc = fixture("session-lmstudio")
    doc["entries"][1]["content"][0]["truncated"] = True
    selection = select(db, StubApsis(doc), cfg, home)
    (session,) = selection.sessions
    assert session.source == "lmstudio"
    entries = session.read_entries()
    assert entries[1].truncated
    assert not entries[0].truncated


def test_unattributed_server_warnings_go_to_the_report_only(db, home, cfg):
    doc = fixture("session-lmstudio")
    doc["entries"].append(
        {
            "role": "system",
            "unattributed": True,
            "content": [{"type": "error", "message": "model crashed"}],
        }
    )
    selection = select(db, StubApsis(doc), cfg, home)
    (session,) = selection.sessions
    assert all(not e.unattributed for e in session.entries())
    assert all("model crashed" not in e.text for e in session.read_entries())
    texts = json.dumps(selection.server_warnings)
    assert "model crashed" in texts
    assert "2026-03/2026-03-05.1.log" in texts  # the unattributed_summary


# Unparseable sessions (2.7)


def test_unparseable_sessions_are_skipped_with_a_reason(db, home):
    cfg = config.build_config({"budget": {"sessions": 1}})
    apsis = StubApsis(
        make_doc("claude", "good", days_ago(3)),
        failing={("claude", "broken"): "malformed transcript"},
    )
    selection = select(db, apsis, cfg, home)
    assert selected_ids(selection) == ["good"]
    (skipped,) = selection.skipped
    assert (skipped.source, skipped.id) == ("claude", "broken")
    assert "malformed transcript" in skipped.reason


def test_listing_warnings_are_kept_for_the_report(db, home, cfg):
    warning = {"code": "source_failed", "message": "kiro failed", "source": "kiro-ide"}
    selection = select(db, StubApsis(listing_warnings=[warning]), cfg, home)
    assert selection.warnings == [warning]


def test_selection_changes_no_state(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "a", days_ago(2)), failing={("claude", "b"): "bad"}
    )
    before = db.total_changes
    select(db, apsis, cfg, home)
    assert db.total_changes == before


# Scoped dreams (2.8)


def test_a_repository_scope_selects_only_that_repository(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "in", days_ago(1), project="/work/repo"),
        make_doc("claude", "out", days_ago(2), project="/work/other"),
    )
    add_seen(db, "claude", "in", entries_read=1)
    request = sessions.SelectionRequest(scope=sessions.Scope(project="/work/repo"))
    selection = select(db, apsis, cfg, home, request)
    assert selected_ids(selection) == ["in"]
    # A scope is not an explicit selection: processed content is not re-read.
    assert selection.sessions[0].start == 1


def test_a_workflow_scope_selects_only_sessions_registered_with_it(db, home, cfg):
    apsis = StubApsis(
        make_doc("claude", "spec", days_ago(1)),
        make_doc("claude", "other", days_ago(2)),
    )
    db.execute(
        "INSERT INTO registrations (tool, run_ref, source, session_id, workflow,"
        " registered_at) VALUES ('orbit', 'r1', 'claude', 'spec', 'starwave', ?)",
        (iso(NOW),),
    )
    request = sessions.SelectionRequest(scope=sessions.Scope(workflow="starwave"))
    selection = select(db, apsis, cfg, home, request)
    assert selected_ids(selection) == ["spec"]
    assert selection.sessions[0].attribution.workflow == "starwave"


def _lens(lens_id, **kwargs):
    return Lens(
        id=lens_id, version=1, look_for="x", may_propose=("knowledge",), **kwargs
    )


def test_a_scope_keeps_only_applicable_lenses(cfg):
    lenses = [
        _lens("general"),
        _lens("repo", projects=("/work/repo",)),
        _lens("other-repo", projects=("/work/other",)),
        _lens("spec", workflows=("starwave",)),
        _lens("other-flow", workflows=("orbit",)),
    ]
    by_repo = sessions.scope_lenses(lenses, sessions.Scope(project="/work/repo"), cfg)
    assert [lens.id for lens in by_repo] == ["general", "repo", "spec", "other-flow"]
    by_flow = sessions.scope_lenses(lenses, sessions.Scope(workflow="starwave"), cfg)
    assert [lens.id for lens in by_flow] == ["general", "repo", "other-repo", "spec"]
    assert sessions.scope_lenses(lenses, None, cfg) == lenses


# Splitting (2.6)


def _read_entry(index, text):
    return sessions.ReadEntry(location=f"e{index}", role="user", text=text)


def test_reader_parts_are_sized_for_the_smallest_reader_context():
    cfg = config.build_config(
        {
            "backends": {
                "small": {"harness": "lmstudio", "context_tokens": 8000},
                "big": {"harness": "claude", "context_tokens": 200000},
            },
            "roles": {"reader": {"backends": ["big", "small"]}},
        }
    )
    assert roles.input_tokens(cfg, "reader", "x" * 4_000) == 8_000 - 2_000 - 1_000


def test_a_session_that_fits_is_one_part():
    entries = [_read_entry(i, "short text") for i in range(5)]
    assert sessions.split_entries(entries, 10_000) == [entries]


def test_an_oversized_entry_is_cut_with_a_truncation_marker():
    entries = [_read_entry(0, "a" * 50), _read_entry(1, "b" * 5000)]
    parts = sessions.split_entries(entries, 200)
    assert [[e.location for e in part] for part in parts] == [["e0"], ["e1"]]
    cut = parts[1][0]
    assert cut.truncated
    assert cut.text.startswith("b" * 100)
    assert sessions.TRUNCATION_MARKER in cut.text
    assert sessions.part_tokens(parts[1]) <= 200


def test_a_context_too_small_for_any_entry_is_an_error():
    with pytest.raises(DreamingError) as excinfo:
        sessions.split_entries([_read_entry(0, "x" * 1000)], 5)
    assert excinfo.value.code == "context_too_small"


@settings(max_examples=200, deadline=None)
@given(
    texts=st.lists(st.text(min_size=0, max_size=3000), min_size=1, max_size=30),
    max_tokens=st.integers(min_value=60, max_value=2000),
)
def test_every_entry_lands_in_exactly_one_part_and_each_part_fits(texts, max_tokens):
    entries = [_read_entry(i, text) for i, text in enumerate(texts)]
    parts = sessions.split_entries(entries, max_tokens)
    flat = [entry for part in parts for entry in part]
    assert [e.location for e in flat] == [e.location for e in entries]
    assert all(part for part in parts)
    for part in parts:
        assert sessions.part_tokens(part) <= max_tokens
    for original, placed in zip(entries, flat, strict=True):
        if placed.truncated:
            kept = placed.text[: placed.text.rindex(sessions.TRUNCATION_MARKER)]
            assert original.text.startswith(kept)
        else:
            assert placed == original


def test_entry_text_renders_every_block_kind():
    entry = sessions.Entry(
        index=0,
        role="assistant",
        content=[
            {"type": "text", "text": "Plain."},
            {"type": "thinking", "text": "Musing."},
            {"type": "tool_use", "name": "shell", "input": {"command": "ls"}},
            {"type": "tool_result", "content": "a.txt", "is_error": True},
            {"type": "plan", "items": [{"text": "step", "completed": True}]},
            {"type": "error", "message": "boom"},
            {"type": "usage", "basis": "x", "counters": {"in": 1}},
            {"type": "hologram", "data": 1},
        ],
    )
    text = sessions.entry_text(entry)
    for expected in ("Plain.", "Musing.", "shell", '"ls"', "a.txt", "step", "boom"):
        assert expected in text
    assert "counters" not in text and "hologram" not in text

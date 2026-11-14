import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dreaming.config import Lens, load_config
from dreaming.sessions import (
    ContentBlock,
    Entry,
    Listing,
    SessionDocument,
    SessionError,
    SessionMetadata,
    SessionScope,
    mark_read,
    record_selection,
    select_sessions,
    split_session,
)
from dreaming.store import Store

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
LENS = Lens("preferences", "1", "Preferences", ("knowledge",))


def document(sid, *, source="claude", count=3, **metadata):
    return SessionDocument(
        SessionMetadata(source=source, id=sid, created_at="2026-11-10T12:00:00Z", **metadata),
        tuple(
            Entry("user", (ContentBlock("text", {"type": "text", "text": f"message {i}"}),))
            for i in range(count)
        ),
        (),
    )


class Source:
    def __init__(self, *documents):
        self.documents = {(d.metadata.source, d.metadata.id): d for d in documents}
        self.rows = [d.metadata for d in documents]

    def list(self):
        return Listing({"all_projects": True}, tuple(self.rows), ())

    def open(self, sid, source):
        value = self.documents[source, sid]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def selection_env(tmp_path):
    config = load_config(tmp_path / "config.toml")
    with Store(tmp_path / "private") as store:
        yield config, store


def select(source, env, **kwargs):
    config, store = env
    return select_sessions(
        source, store, config, run_id="current", lenses=(LENS,), now=NOW, **kwargs
    )


def test_selection_newest_first_budget_and_no_writes(selection_env):
    source = Source(document("old"), document("new"), document("middle"))
    source.rows[1] = replace(source.rows[1], file_modified_at="2026-11-13T12:00:00Z")
    source.rows[2] = replace(source.rows[2], file_modified_at="2026-11-12T12:00:00Z")
    store = selection_env[1]
    before = store.connection.total_changes
    result = select(source, selection_env, max_sessions=2)
    assert [s.document.metadata.id for s in result.sessions] == ["new", "middle"]
    assert store.connection.total_changes == before


def test_incremental_growth_lens_versions_and_explicit_rereads(selection_env):
    doc = document("s")
    source = Source(doc)
    selected = select(source, selection_env).sessions[0]
    mark_read(selection_env[1], selected, selection_env[0])
    assert not select(source, selection_env).sessions
    source.documents["claude", "s"] = document("s", count=5)
    tail = select(source, selection_env).sessions[0]
    assert tail.start_entry == 3
    assert len(tail.document.entries[tail.start_entry :]) == 2
    assert select(source, selection_env, reread=True).sessions[0].start_entry == 0
    assert select(source, selection_env, since="2026-11-01").sessions[0].start_entry == 0
    config, store = selection_env
    changed = select_sessions(
        source, store, config, run_id="current", lenses=(replace(LENS, version="2"),), now=NOW
    )
    assert changed.sessions[0].start_entry == 0


def test_current_dream_recent_and_previous_dream(selection_env):
    config, store = selection_env
    home = store.path.parent
    source = Source(
        document("thread", source="lmstudio", thread_id="current"),
        document("call"),
        document("cwd", project=str(home / "runs" / "current")),
        document("old-dream", project=str(home / "runs" / "previous")),
        document("recent"),
    )
    source.rows[-1] = replace(source.rows[-1], file_modified_at=NOW.isoformat())
    store.connection.execute("INSERT INTO runs(id,status) VALUES ('current','running')")
    store.connection.execute(
        "INSERT INTO steps(run_id,key,phase) VALUES ('current','probe','Read')"
    )
    store.connection.execute(
        "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,session_ref) "
        "VALUES ('c','current','probe',1,'reader','fake','call')"
    )
    result = select(source, selection_env)
    assert [s.document.metadata.id for s in result.sessions] == ["old-dream"]
    assert result.sessions[0].subject == "dreaming"
    assert {s.reason for s in result.skipped} >= {"current_dream", "recent"}


def test_thread_dedupe_prefers_harness_and_keeps_only_server_metadata(selection_env):
    harness = document("h", source="codex", thread_id="t", project="/missing/repo")
    log = replace(
        document("l", source="lmstudio", thread_id="t", provider="lmstudio"),
        requests=({"id": "request", "parameters": {"temperature": 0.2}},),
        entries=(
            Entry("assistant", (ContentBlock("text", {"text": "warning"}),), unattributed=True),
        ),
        unattributed_summary=({"file": "server.log", "counts": {"warn": 1}},),
    )
    result = select(Source(log, harness), selection_env)
    assert len(result.sessions) == 1
    selected = result.sessions[0]
    assert selected.document.metadata.source == "codex"
    assert selected.document.entries == harness.entries
    assert selected.document.requests[0]["id"] == "request"
    assert selected.document.metadata.provider == "lmstudio"
    assert selected.document.server_metadata[0].provider == "lmstudio"
    assert len(selected.copies) == 2
    assert result.diagnostics
    mark_read(selection_env[1], selected, selection_env[0])
    assert not select(Source(log), selection_env).sessions


def test_log_only_is_marked_truncated_and_missing_threads_are_not_deduped(selection_env):
    result = select(Source(document("l", source="lmstudio"), document("h")), selection_env)
    assert len(result.sessions) == 2
    log = next(s for s in result.sessions if s.document.metadata.source == "lmstudio")
    assert all(b.data.get("truncated") for e in log.document.entries for b in e.content)


def test_previous_dream_identified_by_run_thread_before_call_ref_saved(selection_env):
    selection_env[1].connection.execute(
        "INSERT INTO runs(id,status) VALUES ('previous','complete')"
    )
    selected = select(
        Source(document("request", source="lmstudio", thread_id="previous")), selection_env
    ).sessions[0]
    assert selected.subject == "dreaming"
    assert not selected.attribution.interactive


def test_failed_parse_is_reported_persisted_explicitly_and_does_not_stop_selection(selection_env):
    source = Source(document("bad"), document("good"))
    source.documents["claude", "bad"] = SessionError("invalid_document", "bad JSON")
    result = select(source, selection_env)
    assert len(result.sessions) == 1
    assert result.skipped[0].reason.startswith("invalid_document")
    record_selection(selection_env[1], result)
    assert selection_env[1].connection.execute(
        "SELECT state,reason FROM sessions_seen WHERE session_id='bad'"
    ).fetchone() == ("skipped", "invalid_document: bad JSON")


def test_scopes_dates_and_applicable_lenses(selection_env):
    source = Source(document("a", project="/missing/a"), document("b", project="/missing/b"))
    result = select(source, selection_env, scope=SessionScope(project="/missing/a"))
    assert [s.document.metadata.id for s in result.sessions] == ["a"]
    assert not select(source, selection_env, until="2026-11-10").sessions
    assert not select(source, selection_env, since="2026-11-11").sessions
    config, store = selection_env
    lenses = (LENS, replace(LENS, id="other", projects=("/missing/b",)))
    selected = select_sessions(
        source,
        store,
        config,
        run_id="r",
        lenses=lenses,
        scope=SessionScope(project="/missing/a"),
        now=NOW,
    ).sessions[0]
    assert [lens.id for lens in selected.lenses] == ["preferences"]


@given(st.lists(st.text(max_size=250), max_size=20), st.integers(min_value=180, max_value=600))
def test_split_parts_cover_every_entry_once_and_fit(texts, limit):
    doc = replace(
        document("split"),
        entries=tuple(
            Entry("user", (ContentBlock("text", {"type": "text", "text": text}),)) for text in texts
        ),
    )
    parts = split_session(doc, context_tokens=limit + 30, prompt_tokens=20, output_tokens=10)
    assert [i for p in parts for i in range(p.start_entry, p.end_entry)] == list(range(len(texts)))
    for part in parts:
        assert part.estimated_tokens == (len(part.payload.encode()) + 3) // 4
        assert part.estimated_tokens <= limit
        assert json.loads(part.payload)["entries"]


def test_oversized_entry_truncation_and_smallest_fallback(selection_env):
    config, _ = selection_env
    doc = replace(
        document("big", count=1),
        entries=(Entry("user", (ContentBlock("text", {"type": "text", "text": "界" * 10000}),)),),
    )
    limit = min(config.backends[b].context_tokens for b in config.roles["reader"])
    part = split_session(doc, context_tokens=limit, prompt_tokens=limit - 250)[0]
    assert "[TRUNCATED]" in part.payload
    assert part.estimated_tokens <= 250
    with pytest.raises(SessionError, match="context_too_small"):
        split_session(doc, context_tokens=10, prompt_tokens=10)

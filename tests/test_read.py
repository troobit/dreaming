"""Read steps and the deterministic clustering pass (2.6, 2.7, 2.11, 4.3, 4.7,
6.1, 6.5, 6.6)."""

import json
import random

from dreamkit import (
    ScriptedModel,
    answering,
    build_config,
    dream,
    instructions,
    observation,
)
from hypothesis import given
from hypothesis import strategies as st
from runkit import Crash, FakeClock, LocalPhase, add_run, make_runner, step_states
from sessiondocs import (
    StubApsis,
    add_observation,
    add_seen,
    days_ago,
    make_doc,
    make_entry,
    select,
)

from dreaming import knowledge, observe, review, sessions
from dreaming.run import Context

LMSTUDIO = "lmstudio-thread-1"


def doc(sid, *texts, days=2, **meta):
    when = days_ago(days)
    entries = [
        make_entry("user", text, when, id=f"{sid}-u{i}") for i, text in enumerate(texts)
    ]
    entries.append(make_entry("assistant", "sure", when, id=f"{sid}-a"))
    return make_doc("claude", sid, when, entries=entries, **meta)


def observations(db):
    return db.execute(
        "SELECT * FROM observations ORDER BY session_id, entry_id"
    ).fetchall()


def seen(db):
    rows = db.execute("SELECT * FROM sessions_seen ORDER BY session_id")
    return {row["session_id"]: row for row in rows}


# Planning


def test_read_steps_come_from_the_selection_and_review_waits_for_all(db, home):
    cfg = build_config()
    apsis = StubApsis(
        doc("s1", "use uv"),
        doc("s2", "use uv", days=3),
        failing={("claude", "broken"): "cannot parse"},
    )
    add_run(db, "dream-test", cfg)
    runner = make_runner(
        db, home, "dream-test", cfg, phases={}, model=None, clock=FakeClock()
    )
    planned = observe.ReadPhase(instructions(cfg), apsis).plan(Context(runner, None))
    assert [step.key for step in planned] == [
        observe.read_key("claude", "s1"),
        observe.read_key("claude", "s2"),
        observe.read_key("claude", "broken"),
        "review",
    ]
    reads = planned[:3]
    assert {step.phase for step in reads} == {"read"}
    assert planned[-1].depends_on == tuple(step.key for step in reads)
    refs = [json.loads(step.input_ref) for step in reads]
    assert refs[2] == {"source": "claude", "id": "broken", "skipped": "cannot parse"}
    assert refs[0]["attribution"]["project"] == "/work/repo"
    assert refs[0]["copies"][0]["id"] == "s1"


def test_planning_keeps_selection_notes_and_marks_evidence_by_its_source(db, home):
    """The report lists what selection could not attribute (2.3, 2.4, 2.9), and
    evidence of a session no longer listed is marked unavailable (13.7)."""
    cfg = build_config()
    warning = {"code": "unreadable", "message": "a log could not be read"}
    apsis = StubApsis(
        doc("s1", "use uv", origin="mystery-client"), listing_warnings=[warning]
    )
    add_seen(db, "claude", "gone")
    add_observation(db, "ev-gone", "claude", "gone")
    add_run(db, "dream-test", cfg)
    runner = make_runner(
        db, home, "dream-test", cfg, phases={}, model=None, clock=FakeClock()
    )
    observe.ReadPhase(instructions(cfg), apsis).plan(Context(runner, None))
    notes = json.loads(
        db.execute("SELECT selection_notes FROM runs").fetchone()["selection_notes"]
    )
    assert notes == {
        "unmapped_domains": [["claude", "/work/repo"]],
        "unmapped_origins": [["claude", "mystery-client"]],
        "warnings": [warning],
        "server_warnings": [],
    }
    (marked,) = db.execute("SELECT availability FROM observations").fetchall()
    assert marked["availability"] == "unavailable"


# Reading


def test_a_dream_reads_each_session_into_gated_observations(db, home):
    cfg = build_config()
    apsis = StubApsis(
        doc("s1", "key: prefers uv"), doc("s2", "key: prefers uv", days=3)
    )
    model = ScriptedModel(answering())
    assert dream(db, home, cfg, apsis, model) == "complete"

    rows = observations(db)
    assert [row["session_id"] for row in rows] == ["s1", "s2"]
    assert {(row["backend"], row["model"]) for row in rows} == {("fake", "fake-model")}
    assert {row["pattern_key"] for row in rows} == {"prefers uv"}
    states = seen(db)
    assert states["s1"]["state"] == "read"
    assert json.loads(states["s1"]["lens_versions"])["preferences"] == 1
    assert states["s1"]["entries_read"] == 2
    # Each reader call names the sessions its input held (2.11).
    refs = [
        json.loads(row[0])
        for row in db.execute("SELECT input_ref FROM calls WHERE role = 'reader'")
    ]
    assert sorted(refs, key=str) == [
        {"sessions": [["claude", "s1"]]},
        {"sessions": [["claude", "s2"]]},
    ]
    # The reader saw only its session's entries, never another's.
    assert all(len(data["entries"]) == 2 for data in model.of("reader"))


def test_observations_failing_the_gate_are_rejected_and_counted(db, home):
    cfg = build_config()

    def reader(data):
        (user, _) = data["entries"]
        return {
            "observations": [
                observation(user, "use uv", "prefers uv"),
                observation(user, "words never written", "invented"),
                observation(user, "use uv", "wrong lens", lens="no-such-lens"),
            ]
        }

    dream(
        db, home, cfg, StubApsis(doc("s1", "use uv")), ScriptedModel(answering(reader))
    )
    assert [row["pattern_key"] for row in observations(db)] == ["prefers uv"]
    rejected = db.execute(
        "SELECT what, subject, reason, detail FROM rejections ORDER BY id"
    ).fetchall()
    assert [row["what"] for row in rejected] == ["observation", "observation"]
    assert "does not match" in rejected[0]["reason"]
    assert "no-such-lens" in rejected[1]["reason"]
    assert rejected[0]["subject"] == "claude:s1#s1-u0"


def test_reading_a_session_again_under_the_same_lenses_adds_nothing(db, home):
    cfg = build_config()
    apsis = StubApsis(doc("s1", "use uv"))
    dream(db, home, cfg, apsis, ScriptedModel(answering()), run_id="dream-1")
    dream(
        db,
        home,
        cfg,
        apsis,
        ScriptedModel(answering()),
        run_id="dream-2",
        selection={"reread": True},
    )
    assert len(observations(db)) == 1


def test_a_session_larger_than_the_context_is_read_in_parts(db, home):
    cfg = build_config(context_tokens=4_000)
    texts = [f"remark {i}: " + "detail " * 300 for i in range(4)]
    model = ScriptedModel(answering(), context_tokens=4_000)
    dream(db, home, cfg, StubApsis(doc("s1", *texts)), model)
    parts = model.of("reader")
    assert len(parts) > 1
    assert [p["session"]["part"] for p in parts] == [
        f"{n}/{len(parts)}" for n in range(1, len(parts) + 1)
    ]
    # Every user entry was quoted from the part that held it.
    assert len(observations(db)) == 4
    keys = [row[0] for row in db.execute("SELECT step_key FROM calls ORDER BY id")]
    assert keys[: len(parts)] == [
        f"{observe.read_key('claude', 's1')}#p{n}" for n in range(1, len(parts) + 1)
    ]


def test_failed_inputs_are_listed_and_not_retried_unless_selected_again(db, home):
    cfg = build_config()
    apsis = StubApsis(doc("s1", "use uv"))
    broken = ScriptedModel(answering(reader=lambda data: {"wrong": True}))
    dream(db, home, cfg, apsis, broken, run_id="dream-1")
    key = observe.read_key("claude", "s1")
    assert step_states(db, "dream-1")[key] == "failed"
    row = seen(db)["s1"]
    assert row["state"] == "failed"
    assert "observations" in row["reason"]
    assert len(broken.of("reader")) == 2  # one retry, then the input fails

    cfg_now = build_config()
    assert select(db, apsis, cfg_now, home).sessions == []
    again = select(db, apsis, cfg_now, home, sessions.SelectionRequest(reread=True))
    assert [s.id for s in again.sessions] == ["s1"]


def test_an_unparseable_session_is_recorded_as_skipped(db, home):
    cfg = build_config()
    apsis = StubApsis(failing={("claude", "broken"): "not a session document"})
    dream(db, home, cfg, apsis, ScriptedModel(answering()))
    row = seen(db)["broken"]
    assert (row["state"], row["reason"]) == ("skipped", "not a session document")
    assert step_states(db)[observe.read_key("claude", "broken")] == "skipped"


def test_a_session_forgotten_before_its_read_step_is_not_read(db, home):
    cfg = build_config()
    apsis = StubApsis(doc("s1", "use uv"))
    add_seen(db, "claude", "s1", entries_read=0)
    add_run(db, "dream-test", cfg, selection={"reread": True})
    read = observe.ReadPhase(instructions(cfg), apsis)

    def plan_then_forget(ctx):
        planned = read.plan(ctx)
        knowledge.forget_session(db, "s1")
        return planned

    model = ScriptedModel(answering())
    make_runner(
        db,
        home,
        "dream-test",
        cfg,
        phases={"read": read, "review": LocalPhase()},
        model=model,
        clock=FakeClock(),
        planner=plan_then_forget,
    ).execute()
    assert model.of("reader") == []
    assert step_states(db)[observe.read_key("claude", "s1")] == "skipped"
    assert observations(db) == []


def test_forgetting_takes_effect_before_the_next_part_is_read(db, home):
    cfg = build_config(context_tokens=4_000)
    texts = [f"remark {i}: " + "detail " * 300 for i in range(4)]
    apsis = StubApsis(doc("s1", *texts))
    add_seen(db, "claude", "s1", entries_read=0)

    def forget_after_first(role, data):
        if role == "reader":
            db.execute("UPDATE sessions_seen SET forgotten = 1")

    model = ScriptedModel(answering(), context_tokens=4_000, before=forget_after_first)
    dream(db, home, cfg, apsis, model, selection={"reread": True})
    assert len(model.of("reader")) == 1
    assert step_states(db)[observe.read_key("claude", "s1")] == "skipped"
    assert observations(db) == []


def test_a_crash_after_the_call_resumes_with_the_stored_answer(db, home):
    cfg = build_config()
    apsis = StubApsis(doc("s1", "use uv"))
    found = instructions(cfg)

    class CrashingRead(observe.ReadPhase):
        crashed = False

        def apply(self, conn, step, result):
            outcome = super().apply(conn, step, result)
            if not CrashingRead.crashed:
                CrashingRead.crashed = True
                raise Crash
            return outcome

    model = ScriptedModel(answering())
    add_run(db, "dream-test", cfg)
    read = CrashingRead(found, apsis)

    def runner():
        return make_runner(
            db,
            home,
            "dream-test",
            cfg,
            phases={"read": read, "review": LocalPhase()},
            model=model,
            clock=FakeClock(),
            planner=read.plan,
        )

    try:
        runner().execute()
    except Crash:
        pass
    assert observations(db) == []
    assert runner().execute() == "complete"
    assert len(model.of("reader")) == 1
    assert len(observations(db)) == 1


def test_a_dream_call_whose_input_held_a_forgotten_session_is_not_selected(db, home):
    cfg = build_config()
    call_project = str(home / "runs" / "dream-old" / "calls")
    apsis = StubApsis(
        doc("s1", "use uv", days=5),
        doc("call-1", "read this session", days=3, project=call_project),
    )
    add_run(db, "dream-old", cfg)
    db.execute(
        "INSERT INTO calls (run, step_key, attempt, role, backend, session_ref,"
        " input_ref, started_at) VALUES ('dream-old', 'read:x', 1, 'reader', 'fake',"
        " 'call-1', ?, '2026-11-10')",
        (observe.call_input_ref([{"source": "claude", "id": "s1"}]),),
    )
    add_seen(db, "claude", "s1")
    chosen = select(db, apsis, cfg, home, sessions.SelectionRequest(reread=True))
    assert {s.id: s.attribution.subject for s in chosen.sessions} == {
        "call-1": "dreaming",
        "s1": "user",
    }
    knowledge.forget_session(db, "s1")
    chosen = select(db, apsis, cfg, home, sessions.SelectionRequest(reread=True))
    assert chosen.sessions == []


def test_per_session_counts_stay_after_the_transcript_is_gone(db, home):
    cfg = build_config()
    apsis = StubApsis(
        doc("s1", "key: prefers uv"), doc("s2", "key: prefers uv", days=3)
    )
    dream(db, home, cfg, apsis, ScriptedModel(answering()))
    before = knowledge.pattern_signals(db)
    assert sum(s.observations for s in before.values()) == 2
    apsis.docs.clear()  # the harness deleted its transcripts
    db.execute("UPDATE sessions_seen SET availability = 'unavailable'")
    assert knowledge.pattern_signals(db) == before


# Clustering (6.5)


def row(evidence_id, lens, key, *, day="2026-11-01", session="t1"):
    return {
        "evidence_id": evidence_id,
        "lens": lens,
        "pattern_key": key,
        "observed_on": day,
        "interaction": session,
        "project": "/p",
    }


def test_clusters_group_by_lens_and_normalised_key():
    rows = [
        row("e1", "preferences", "Prefers uv"),
        row("e2", "preferences", "preferences: prefers  UV"),
        row("e3", "skill-use", "prefers uv"),
        row("e4", "preferences", "prefers pip"),
        row("e5", "preferences", " "),
    ]
    clusters = review.cluster(rows)
    assert [(c.ref, [r["evidence_id"] for r in c.rows]) for c in clusters] == [
        ("preferences/prefers pip", ["e4"]),
        ("preferences/prefers uv", ["e1", "e2"]),
        ("skill-use/prefers uv", ["e3"]),
    ]


keys = st.sampled_from(["prefers uv", "Prefers UV", "skill misfires", "stalls on ci"])
lenses = st.sampled_from(["preferences", "skill-use"])


@given(
    st.lists(st.tuples(lenses, keys, st.integers(1, 9)), min_size=1, max_size=20),
    st.randoms(),
)
def test_shuffling_observations_yields_the_same_clusters(specs, rng):
    rows = [
        row(f"e{i}", lens, key, day=f"2026-11-0{day}")
        for i, (lens, key, day) in enumerate(specs)
    ]
    shuffled = list(rows)
    random.Random(rng.random()).shuffle(shuffled)
    assert review.cluster(shuffled) == review.cluster(rows)

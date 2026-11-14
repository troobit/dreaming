"""The Review step with a fake reviewer (4.3, 4.5, 6.5, 7.5, 9.13, 9.14, 11.3,
11.4, 11.6, 13.8)."""

import json
from pathlib import Path

import pytest
from dreamkit import EMPTY_REVIEW, ScriptedModel, build_config, instructions
from hypothesis import given, settings
from hypothesis import strategies as st
from runkit import Crash, FakeClock, LocalPhase, add_run, make_runner, step_states
from sessiondocs import add_observation

from dreaming import review, steps, store
from dreaming.steps import NewStep

RUN = "dream-now"


@pytest.fixture
def cfg():
    return build_config(
        repositories={"agentic": {"path": "/work/agentic"}},
        thresholds={"min_sessions": 3},
    )


def pattern(db, pattern_id, key, *, lens="preferences", run=None, reviewed=None):
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at, created_run,"
        " last_reviewed_run) VALUES (?, ?, ?, '2026-10-01', ?, ?)",
        (pattern_id, key, lens, run, reviewed),
    )


def observe(db, eid, session, key, *, pattern_id=None, day="2026-11-10", **fields):
    db.execute(
        "INSERT OR IGNORE INTO sessions_seen (source, session_id, state)"
        " VALUES ('claude', ?, 'read')",
        (session,),
    )
    fields.setdefault("kind", "tool_result")
    fields.setdefault("lens", "preferences")
    fields.setdefault("project", "/work/repo")
    add_observation(
        db, eid, "claude", session, pattern_id=pattern_id, observed_on=day, **fields
    )
    db.execute(
        "UPDATE observations SET pattern_key = ? WHERE evidence_id = ?", (key, eid)
    )
    return eid


def spread(db, prefix, key, count, **fields):
    """`count` observations of `key`, each in its own session."""
    return [
        observe(db, f"{prefix}{i}", f"{prefix}-s{i}", key, **fields)
        for i in range(count)
    ]


def earlier_run(db, run_id, started_at, cfg, **fields):
    add_run(db, run_id, cfg, status="complete", started_at=started_at, **fields)


def run_review(db, cfg, model, *, run_id=RUN, phases=None, home=Path("/unused")):
    add_run(db, run_id, cfg, started_at="2026-11-15T09:00:00+00:00")
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases=phases
        or {
            "review": review.ReviewPhase(instructions(cfg)),
            "research": LocalPhase(),
            "author": LocalPhase(),
        },
        model=model,
        clock=FakeClock(),
        planner=lambda ctx: [NewStep("review", "review")],
    )
    return runner.execute()


def reviewer(answer):
    def call(role, data):
        assert role == "reviewer"
        return {**EMPTY_REVIEW, **(answer(data) if callable(answer) else answer)}

    return ScriptedModel(call)


def create(supporting, *, pattern=None, content="prefers uv", **fields):
    return {
        "op": "create",
        "item_id": None,
        "base_version": None,
        "kind": "preference",
        "content": content,
        "scope": {
            "domains": [],
            "projects": [],
            "workflows": [],
            "global": False,
            "unresolved": False,
        },
        "confidence": "medium",
        "confidence_basis": "three sessions",
        "supporting": list(supporting),
        "contradicting": [],
        "pattern": pattern,
        "reason": "recurs",
        **fields,
    }


def candidate(pattern_ref, evidence, *, proposal="change", repo="agentic"):
    return {
        "pattern": pattern_ref,
        "proposal": proposal,
        "repo": repo if proposal == "change" else None,
        "summary": "add a uv rule",
        "evidence": list(evidence),
        "reason": "recurs",
    }


def rejections(db, what=None):
    rows = db.execute("SELECT * FROM rejections ORDER BY id").fetchall()
    return [row for row in rows if what is None or row["what"] == what]


# What the reviewer sees


def test_the_reviewer_sees_bounded_ranked_summaries_and_leftover_counts(db):
    cfg = build_config(context_tokens=6_000)
    for i in range(30):
        pattern(db, f"p-{i:02d}", f"old {i}", lens="skill-use" if i % 2 else "stalls")
        spread(db, f"o{i:02d}-", f"old {i}", 1 + i % 4, pattern_id=f"p-{i:02d}")
    for i in range(40):
        spread(db, f"n{i:02d}-", f"new thing {i}", 1)
    model = reviewer({})
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    assert data["new_clusters"]
    assert data["patterns"]
    omitted = data["omitted"]
    assert omitted["new_clusters"] == 40 - len(data["new_clusters"])
    assert sum(omitted["patterns_by_lens"].values()) == 30 - len(data["patterns"])
    # Existing patterns come in rank order: more distinct sessions first.
    sessions = [p["sessions"] for p in data["patterns"]]
    assert sessions == sorted(sessions, reverse=True)
    # New clusters are named by lens and key; evidence is ids and short quotes.
    first = data["new_clusters"][0]
    assert first["key"] == "preferences/new thing 0"
    assert set(first["evidence"][0]) == {
        "id",
        "quote",
        "kind",
        "modality",
        "outcome",
        "session",
        "project",
        "date",
    }


def test_a_summary_shows_one_observation_per_session_first():
    rows = [
        {
            "evidence_id": f"e{i}",
            "interaction": f"t{i % 3}",
            "observed_on": f"2026-11-{10 + i:02d}",
            "quote": "q",
            "kind": "tool_result",
            "modality": "asserted",
            "outcome": "unknown",
            "project": None,
        }
        for i in range(15)
    ]
    shown = review.summary_evidence(rows, 40)
    assert len(shown) == review.SUMMARY_EVIDENCE
    assert {e.session for e in shown} == {"t0", "t1", "t2"}
    assert [e.date for e in shown] == sorted(e.date for e in shown)
    assert shown[-1].id == "e14"  # the newest is kept


def test_nothing_new_to_review_makes_no_call(db, cfg):
    pattern(db, "p-uv", "prefers uv", reviewed=None)
    spread(db, "o", "prefers uv", 3, pattern_id="p-uv")
    model = reviewer({})
    assert run_review(db, cfg, model) == "complete"
    assert model.inputs == []
    assert step_states(db, RUN) == {"review": "done"}


def test_unreviewed_patterns_are_boosted_and_marked_reviewed(db, cfg):
    for number in range(1, 5):
        earlier_run(db, f"dream-{number}", f"2026-11-0{number}T00:00:00+00:00", cfg)
    pattern(db, "p-busy", "busy", run="dream-1", reviewed="dream-4")
    spread(db, "b", "busy", 6, pattern_id="p-busy")
    pattern(db, "p-quiet", "quiet", run="dream-1", reviewed="dream-1")
    spread(db, "q", "quiet", 1, pattern_id="p-quiet")
    spread(db, "n", "something new", 1)
    model = reviewer({})
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    # Three dreams since p-quiet's review reach boost_after_dreams = 3.
    assert [p["pattern_id"] for p in data["patterns"]] == ["p-quiet", "p-busy"]
    reviewed = dict(db.execute("SELECT id, last_reviewed_run FROM patterns"))
    assert reviewed["p-quiet"] == reviewed["p-busy"] == RUN


def test_the_reviewer_receives_earlier_run_records_as_counts(db, cfg):
    earlier_run(db, "dream-1", "2026-11-01T00:00:00+00:00", cfg)
    steps.add(db, "dream-1", [NewStep("read:a", "read"), NewStep("read:b", "read")])
    steps.finish(db, "dream-1", "read:a", "done")
    steps.finish(db, "dream-1", "read:b", "failed", "claude:secret-session failed")
    steps.record_rejection(db, "dream-1", "read:a", "observation", "s", "no match")
    spread(db, "n", "something new", 1)
    model = reviewer({})
    run_review(db, cfg, model)
    (record,) = model.of("reviewer")[0]["run_records"]
    assert record["run"] == "dream-1"
    assert record["steps"] == {"read": {"done": 1, "failed": 1}}
    assert record["rejected"] == {"observation": 1}
    assert "secret-session" not in json.dumps(record)


def test_the_reviewer_call_names_the_sessions_its_input_held(db, cfg):
    spread(db, "n", "something new", 2)
    run_review(db, cfg, reviewer({}))
    (ref,) = db.execute("SELECT input_ref FROM calls WHERE role = 'reviewer'")
    assert json.loads(ref[0]) == {"sessions": [["claude", "n-s0"], ["claude", "n-s1"]]}


# Patterns (6.5, 11.4)


def test_exact_keys_join_their_pattern_and_only_the_program_creates_ids(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    observe(db, "e1", "s1", "Prefers  UV")
    spread(db, "n", "new thing", 2)
    spread(db, "m", "another thing", 1)
    model = reviewer(
        {
            "assignments": [
                {"cluster": "preferences/new thing", "pattern_id": None, "reason": ""},
                {
                    "cluster": "preferences/another thing",
                    "pattern_id": "p-invented",
                    "reason": "",
                },
                {
                    "cluster": "preferences/never shown",
                    "pattern_id": None,
                    "reason": "",
                },
            ]
        }
    )
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    assert [c["key"] for c in data["new_clusters"]] == [
        "preferences/another thing",
        "preferences/new thing",
    ]
    assigned = dict(db.execute("SELECT evidence_id, pattern_id FROM observations"))
    assert assigned["e1"] == "p-uv"
    created = {
        row["key"]: row["id"]
        for row in db.execute("SELECT * FROM patterns WHERE id != 'p-uv'")
    }
    assert set(created) == {"new thing", "another thing"}
    assert all(pid.startswith("p-") for pid in created.values())
    assert assigned["n0"] == assigned["n1"] == created["new thing"]
    assert assigned["m0"] == created["another thing"]
    reasons = [row["reason"] for row in rejections(db, "assignment")]
    assert reasons == ["no pattern p-invented", "no such new cluster was shown"]


def test_pattern_merges_keep_the_history_under_one_id(db, cfg):
    pattern(db, "p-a", "uses uv")
    pattern(db, "p-b", "prefers uv")
    spread(db, "a", "uses uv", 1, pattern_id="p-a")
    spread(db, "b", "prefers uv", 1, pattern_id="p-b")
    spread(db, "n", "something new", 1)
    model = reviewer(
        {
            "pattern_merges": [
                {"pattern_ids": ["p-a", "p-b"], "into": "p-a", "reason": "same"},
                {"pattern_ids": ["p-a", "p-x"], "into": "p-a", "reason": "same"},
            ]
        }
    )
    run_review(db, cfg, model)
    merged = dict(
        db.execute("SELECT id, merged_into FROM patterns WHERE id LIKE 'p-_'")
    )
    assert merged == {"p-a": None, "p-b": "p-a"}
    owners = {
        row[0]
        for row in db.execute(
            "SELECT pattern_id FROM observations WHERE evidence_id IN ('a0', 'b0')"
        )
    }
    assert owners == {"p-a"}
    assert [row["reason"] for row in rejections(db, "pattern_merge")] == [
        "no pattern p-x"
    ]


# Knowledge operations (13.8)


def test_operations_are_applied_or_recorded_rejected_with_reasons(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    spread(db, "n", "something new", 1)
    model = reviewer(
        {
            "knowledge": [
                create(ids, pattern="p-uv"),
                create(["ev-invented"], pattern="p-uv", content="made up"),
                create(ids, pattern="p-nowhere", content="lost"),
                create(ids, content="inferred pattern"),
            ]
        }
    )
    run_review(db, cfg, model)
    items = db.execute(
        "SELECT summary, status, pattern_id FROM knowledge_items ORDER BY summary"
    ).fetchall()
    assert [tuple(row) for row in items] == [
        ("inferred pattern", "active", "p-uv"),
        ("prefers uv", "active", "p-uv"),
    ]
    revision = db.execute(
        "SELECT originating_run, actor, backend FROM knowledge_revisions LIMIT 1"
    ).fetchone()
    assert tuple(revision) == (RUN, "dream", "fake")
    reasons = [row["reason"] for row in rejections(db, "knowledge")]
    assert "ev-invented" in reasons[0]
    assert reasons[1] == "no pattern or shown cluster p-nowhere"


def test_items_about_the_user_cannot_rest_on_earlier_dreams_sessions(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv", subject="dreaming")
    spread(db, "n", "something new", 1)
    run_review(db, cfg, reviewer({"knowledge": [create(ids, pattern="p-uv")]}))
    (rejected,) = rejections(db, "knowledge")
    assert "earlier dream" in rejected["reason"]


def test_a_new_cluster_can_carry_an_item_in_the_same_review(db, cfg):
    ids = spread(db, "n", "prefers uv", 3)
    model = reviewer({"knowledge": [create(ids, pattern="preferences/prefers uv")]})
    run_review(db, cfg, model)
    (row,) = db.execute("SELECT status, pattern_id FROM knowledge_items")
    created = db.execute("SELECT id FROM patterns WHERE key = 'prefers uv'").fetchone()
    assert tuple(row) == ("active", created[0])


# Candidates and follow-up steps (4.5, 9.13, 9.14, 11.3)


def test_candidates_get_research_and_author_steps_with_the_review(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    model = reviewer(
        {
            "candidates": [
                candidate("preferences/new", ["n0"]),
                candidate("p-uv", ids),
            ],
            "questions": [
                {
                    "question": "Which rule file covers uv?",
                    "candidate": 1,
                    "reason": "",
                },
                {
                    "question": "What changed in uv 0.9?",
                    "candidate": None,
                    "reason": "",
                },
                {"question": "Unrelated?", "candidate": 0, "reason": ""},
            ],
        }
    )
    spread(db, "n", "new", 1)
    run_review(db, cfg, model)
    (row,) = db.execute("SELECT * FROM candidates")
    assert (row["pattern_id"], row["proposal"], row["repo"]) == (
        "p-uv",
        "change",
        "agentic",
    )
    assert row["position"] == 1
    signals = json.loads(row["signals"])
    assert (signals["sessions"], signals["projects"]) == (3, 1)
    assert signals["score"] > 3
    questions = dict(db.execute("SELECT question, candidate_id FROM questions"))
    assert questions == {
        "Which rule file covers uv?": row["id"],
        "What changed in uv 0.9?": None,
    }
    states = step_states(db, RUN)
    research = sorted(k for k in states if k.startswith("research:"))
    assert len(research) == 2
    author = {s.key: s for s in steps.load(db, RUN)}[f"author:{row['id']}"]
    (asked,) = db.execute(
        "SELECT id FROM questions WHERE candidate_id = ?", (row["id"],)
    )
    assert author.depends_on == (f"research:{asked[0]}",)
    assert author.input_ref == row["id"]
    assert set(states.values()) == {"done"}
    reasons = [r["reason"] for r in rejections(db)]
    assert "its evidence spans 1 of the 3 distinct sessions needed (4.5)" in reasons
    assert "candidate 0 was not accepted" in reasons


def test_a_crash_before_the_commit_leaves_no_candidates_or_steps(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    spread(db, "n", "new", 1)
    model = reviewer({"candidates": [candidate("p-uv", ids)]})

    class CrashingReview(review.ReviewPhase):
        crashed = False

        def apply(self, conn, step, reviewed):
            outcome = super().apply(conn, step, reviewed)
            if not CrashingReview.crashed:
                CrashingReview.crashed = True
                raise Crash
            return outcome

    phases = {
        "review": CrashingReview(instructions(cfg)),
        "research": LocalPhase(),
        "author": LocalPhase(),
    }
    with pytest.raises(Crash):
        run_review(db, cfg, model, phases=phases)
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert set(step_states(db, RUN)) == {"review"}
    runner = make_runner(
        db, Path("/unused"), RUN, cfg, phases=phases, model=model, clock=FakeClock()
    )
    assert runner.execute() == "complete"
    assert len(model.of("reviewer")) == 1  # the stored answer was applied
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_a_file_finding_is_a_candidate_on_its_own(db, cfg):
    pattern(db, "p-ref", "broken reference", lens="broken-references")
    db.execute(
        "INSERT INTO file_findings (evidence_id, repo, revision, path, location,"
        " finding, lens, lens_version, pattern_id) VALUES ('ff1', 'agentic', 'abc',"
        " 'skills/x.md', 'L3', 'links a missing file', 'broken-references', 1,"
        " 'p-ref')"
    )
    spread(db, "n", "new", 1)
    run_review(db, cfg, reviewer({"candidates": [candidate("p-ref", ["ff1"])]}))
    assert db.execute("SELECT pattern_id FROM candidates").fetchone()[0] == "p-ref"


def change(db, change_id, status, *, run="dream-1", reverted_at=None):
    db.execute(
        "INSERT INTO changes (id, run, pattern_id, repo, status, reverted_at,"
        " created_at) VALUES (?, ?, 'p-uv', 'agentic', ?, ?, '2026-11-02')",
        (change_id, run, status, reverted_at),
    )


def test_no_candidate_for_a_pattern_with_an_unmerged_branch(db, cfg):
    earlier_run(db, "dream-1", "2026-11-01T00:00:00+00:00", cfg)
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    change(db, "ch-1", "waiting")
    spread(db, "n", "new", 1)
    model = reviewer({"candidates": [candidate("p-uv", ids)]})
    run_review(db, cfg, model)
    (data,) = model.of("reviewer")
    assert data["patterns"][0]["status"] == "unmerged_change"
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    (rejected,) = rejections(db, "candidate")
    assert "(9.14)" in rejected["reason"]


def test_a_reverted_change_is_proposed_again_once_on_newer_observations(db, cfg):
    earlier_run(db, "dream-1", "2026-11-01T00:00:00+00:00", cfg)
    pattern(db, "p-uv", "prefers uv")
    old = spread(db, "u", "prefers uv", 3, pattern_id="p-uv", day="2026-11-03")
    change(db, "ch-1", "rejected", reverted_at="2026-11-05T10:00:00+00:00")
    spread(db, "n", "new", 1)
    run_review(db, cfg, reviewer({"candidates": [candidate("p-uv", old)]}))
    (rejected,) = rejections(db, "candidate")
    assert "newer than the revert (9.13)" in rejected["reason"]

    newer = spread(db, "v", "prefers uv", 3, pattern_id="p-uv", day="2026-11-12")
    spread(db, "m", "more", 1)
    run_review(
        db, cfg, reviewer({"candidates": [candidate("p-uv", newer)]}), run_id="dream-2"
    )
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1

    spread(db, "w", "even more", 1)
    run_review(
        db, cfg, reviewer({"candidates": [candidate("p-uv", newer)]}), run_id="dream-3"
    )
    assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    assert "proposed again once already" in rejections(db, "candidate")[-1]["reason"]


def test_an_applied_change_waits_for_effect_measurement(db, cfg):
    earlier_run(db, "dream-1", "2026-11-01T00:00:00+00:00", cfg)
    pattern(db, "p-uv", "prefers uv")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    change(db, "ch-1", "applied")
    spread(db, "n", "new", 1)
    run_review(db, cfg, reviewer({"candidates": [candidate("p-uv", ids)]}))
    assert "(11.2)" in rejections(db, "candidate")[0]["reason"]


def test_candidate_rules_for_repositories_lenses_and_learning(db, cfg):
    pattern(db, "p-uv", "prefers uv")
    pattern(db, "p-help", "asks about async", lens="help-requests")
    ids = spread(db, "u", "prefers uv", 3, pattern_id="p-uv")
    asks = spread(
        db,
        "h",
        "asks about async",
        3,
        pattern_id="p-help",
        lens="help-requests",
        kind="user_statement",
        modality="tentative",
    )
    db.execute(
        "INSERT INTO learning (id, summary, pattern_id, disposition, created_at,"
        " updated_at) VALUES ('l1', 'learn async', 'p-help', 'dismissed', '', '')"
    )
    spread(db, "n", "new", 1)
    model = reviewer(
        {
            "candidates": [
                candidate("p-uv", ids, repo="unknown-repo"),
                candidate("p-uv", ids, proposal="learning"),
                candidate("p-help", asks, proposal="learning"),
            ]
        }
    )
    run_review(db, cfg, model)
    reasons = [row["reason"] for row in rejections(db, "candidate")]
    assert reasons == [
        "unknown-repo is not a configured repository",
        "lens preferences may not propose a learning",
        "the pattern already has learning recommendation l1 (dismissed) (15.3)",
    ]


# Determinism (design, Review is deterministic)


@settings(max_examples=15, deadline=None)
@given(st.permutations(list(range(8))))
def test_shuffling_observations_before_review_yields_the_same_knowledge(order):
    def answer(data):
        return {
            "knowledge": [
                create(
                    [e["id"] for e in cluster["evidence"]],
                    pattern=cluster["key"],
                    content=f"about {cluster['key']}",
                )
                for cluster in data["new_clusters"]
            ]
        }

    def knowledge_after(insert_order):
        conn = store.connect(Path(":memory:"))
        store.migrate(conn)
        cfg = build_config(thresholds={"min_sessions": 2})
        specs = [(f"e{i}", f"s{i % 5}", f"key {i % 3}") for i in range(8)]
        for index in insert_order:
            eid, session, key = specs[index]
            observe(conn, eid, session, key)
        run_review(conn, cfg, reviewer(answer))
        rows = conn.execute(
            "SELECT item_id, content, status, supporting, confidence"
            " FROM knowledge_revisions ORDER BY item_id"
        )
        return [tuple(row) for row in rows]

    assert knowledge_after(order) == knowledge_after(range(8))

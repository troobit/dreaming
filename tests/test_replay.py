"""The replay gate: a change to a skill, rule, lens or role prompt is replayed
on past sessions before its checks (9.16–9.18), and a change that received
fetched content never enters the merge (9.19)."""

import json
import shutil

import pytest
from dreamkit import INSTRUCTIONS, ScriptedModel, answering, build_config, dream
from gitkit import commit, head, make_repo
from runkit import Crash, FakeClock, add_run, make_runner
from sessiondocs import (
    StubApsis,
    add_observation,
    add_pattern,
    add_seen,
    days_ago,
    make_doc,
    make_entry,
)

from dreaming import adopt, config, knowledge, replay, report, roles, run
from dreaming.backends import BackendError
from dreaming.budget import tokens_used
from dreaming.steps import NewStep

RUN = "dream-now"
NOW = "2026-11-15T09:00:00+00:00"
PATTERN = "pat-done-claims"
RULE = "rules/testing.md"
# Replayer answers follow an instruction that asks for the whole suite.
FIX = "# Testing\n\nRun the whole suite before saying a change is done.\n"
NOTHING = "# Testing\n\nBe careful with tests.\n"
SOLVED = "I ran the whole suite first."
UNSOLVED = "Done, it works."


def opening(sid):
    return f"fix the flaky test in {sid}"


def session(sid, entries=None):
    when = days_ago(2)
    if entries is None:
        entries = [
            make_entry("user", opening(sid), when, id=f"{sid}-u1"),
            make_entry("assistant", UNSOLVED, when, id=f"{sid}-a1"),
        ]
    return make_doc("claude", sid, when, entries=entries)


def seed(db, *sids):
    """Sessions whose observations show the pattern, newest first by date."""
    add_pattern(db, PATTERN)
    for number, sid in enumerate(sids):
        add_seen(db, "claude", sid)
        add_observation(
            db,
            f"e-{sid}",
            "claude",
            sid,
            pattern_id=PATTERN,
            observed_on=f"2026-11-{10 - number:02d}",
        )
    return [f"e-{sid}" for sid in sids]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path, "agentic", {"check.sh": "exit 0\n", RULE: "# Testing\n"})


def configure(repos, **sections):
    entries = {
        name: {"path": str(path), "test": ["sh check.sh"]}
        for name, path in repos.items()
    }
    return build_config(repositories=entries, adoption={"auto_apply": True}, **sections)


def propose(
    db,
    home,
    cfg,
    files,
    *,
    evidence,
    cid="c1",
    repo="agentic",
    pattern=PATTERN,
    external=0,
):
    """A change committed on the run's dream branch, as the Author step makes it."""
    work = adopt.work_copy(home, cfg, RUN, repo)
    for path, text in files.items():
        (work.path / path).parent.mkdir(parents=True, exist_ok=True)
        (work.path / path).write_text(text)
    change_id = f"ch-{cid}"
    committed = adopt.commit_change(
        home, cfg, RUN, work, change_id, f"Change {cid}", set(files), []
    )
    edits = [{"path": path, "action": "replace"} for path in files]
    db.execute(
        "INSERT INTO changes (id, run, pattern_id, repo, branch, commit_sha, evidence,"
        " edits, external_input, status, summary, commit_message, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)",
        (
            change_id,
            RUN,
            pattern,
            repo,
            adopt.branch_name(RUN),
            committed.sha,
            json.dumps(evidence),
            json.dumps(edits),
            external,
            "Claim done only after the suite",
            f"Change {cid}",
            NOW,
        ),
    )
    return change_id


def verdict(answers):
    return "resolved" if answers[0]["response"] == SOLVED else "recurs"


def model(judged=None, **kwargs):
    """A replayer that follows an instruction asking for the whole suite, and a
    judge that sees the problem resolved only where an answer runs it, unless
    `judged` gives a session's (without, with) judgements."""

    def answer(role, data):
        if role == "replayer":
            followed = "whole suite" in (data["instruction"] or "")
            return {"response": SOLVED if followed else UNSOLVED, "plan": ["reply"]}
        if role == "reviewer":
            (quote,) = data["evidence"]
            sid = quote.removeprefix("quote e-")
            without, with_change = (judged or {}).get(
                sid, (verdict(data["without"]), verdict(data["with"]))
            )
            return {"without": without, "with": with_change, "reason": "as shown"}
        raise AssertionError(f"unexpected {role} call")

    return ScriptedModel(answer, **kwargs)


def adopt_run(db, home, cfg, backend, apsis, *, root=INSTRUCTIONS):
    gate = replay.ReplayGate(roles.load(root, cfg), root, apsis)
    runner = make_runner(
        db,
        home,
        RUN,
        cfg,
        phases={"adopt": adopt.AdoptPhase(gate)},
        model=backend,
        clock=FakeClock(),
        planner=lambda ctx: [NewStep("adopt", "adopt")],
    )
    return runner.execute()


def state(db, change_id="ch-c1"):
    found = db.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
    return found["status"], found["reason"]


def replays(db):
    rows = db.execute("SELECT * FROM replays ORDER BY id")
    return [dict(r) for r in rows]


def checked(db):
    rows = db.execute(
        "SELECT 1 FROM adoption WHERE run = ? AND step = 'checking'", (RUN,)
    )
    return rows.fetchone() is not None


HELD = "change ch-c1 is held by its replay: "


# Skills and rules, replayed on opening prompts (9.16)


def test_a_change_that_resolves_the_problem_is_replayed_then_applied(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    evidence = seed(db, "s1", "s2", "s3")
    propose(db, home, cfg, {RULE: FIX}, evidence=evidence)
    found = model()

    assert adopt_run(db, home, cfg, found, apsis) == "complete"
    assert state(db) == ("applied", None)
    # Each session twice, newest first: with no instruction, then with the
    # changed rule as the only one; the replayer has no tools.
    replayed = found.of("replayer")
    assert [d["opening_prompt"] for d in replayed] == [
        opening(s) for s in ("s1", "s1", "s2", "s2", "s3", "s3")
    ]
    assert [d["instruction"] for d in replayed[::2]] == [None] * 3
    assert {d["instruction"] for d in replayed[1::2]} == {f"# {RULE}\n\n{FIX.strip()}"}
    assert {(role, tuple(tools)) for role, tools in found.tools} == {
        ("replayer", ()),
        ("reviewer", ()),
    }
    # A reviewer-role call judges both answers on the session's own evidence.
    judged = found.of("reviewer")
    assert [d["evidence"] for d in judged] == [
        ["quote e-s1"],
        ["quote e-s2"],
        ["quote e-s3"],
    ]
    assert judged[0]["problem"] == {
        "change": "Claim done only after the suite",
        "pattern": PATTERN,
    }
    assert [a["response"] for a in judged[0]["without"]] == [UNSOLVED]
    assert [a["response"] for a in judged[0]["with"]] == [SOLVED]
    # One row per session, with the calls' tokens, which the budget counts.
    rows = replays(db)
    assert [
        (r["session_id"], r["recurs_without"], r["judgement"], r["backend"], r["model"])
        for r in rows
    ] == [(s, 1, "resolved", "fake", "fake-model") for s in ("s1", "s2", "s3")]
    assert [r["tokens"] for r in rows] == [360] * 3
    assert tokens_used(db, RUN) == 1080
    page = report.render(db, RUN, cfg)
    assert (
        "Replay, an association rather than proof: of 3 sessions run, the problem "
        "recurred without the change in 3 and, of those, with it in 0; 1,080 tokens."
    ) in page


def test_a_change_that_does_nothing_is_held_with_its_whole_branch(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    evidence = seed(db, "s1", "s2", "s3")
    propose(db, home, cfg, {RULE: NOTHING}, evidence=evidence)
    other = propose(
        db, home, cfg, {"README.md": "# agentic\n\nmore\n"}, evidence=["ff-1"], cid="c2"
    )
    before = head(repo)

    adopt_run(db, home, cfg, model(), apsis)
    reason = (
        f"{HELD}the problem recurred with the change in 3 of 3 replayed sessions, "
        "more than 50%"
    )
    assert state(db) == state(db, other) == ("waiting", reason)
    assert [r["judgement"] for r in replays(db)] == ["recurs"] * 3
    # Held before its checks; the checkout is untouched.
    assert not checked(db)
    assert head(repo) == before


@pytest.mark.parametrize(
    "judged, expected",
    [
        ({"s1": ("recurs", "unclear"), "s2": ("recurs", "unclear")}, "waiting"),
        ({"s1": ("recurs", "unclear")}, "applied"),
        ({"s1": ("recurs", "recurs"), "s2": ("recurs", "unclear")}, "waiting"),
    ],
)
def test_an_unclear_judgement_counts_as_recurrence(db, home, repo, judged, expected):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    adopt_run(db, home, cfg, model(judged), apsis)
    assert state(db)[0] == expected


def test_a_session_counts_only_when_the_problem_recurs_without_the_change(
    db, home, repo
):
    cfg = configure({"agentic": repo}, thresholds={"replay_max_recurrence": 0.3})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    # s1 never showed the problem, so its result with the change is not counted:
    # counted, it would be a recurrence share of 1 in 3, above 0.3.
    adopt_run(db, home, cfg, model({"s1": ("resolved", "recurs")}), apsis)
    assert state(db) == ("applied", None)
    assert [r["recurs_without"] for r in replays(db)] == [0, 1, 1]


def test_too_few_sessions_showing_the_problem_without_the_change_hold_it(
    db, home, repo
):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    judged = {"s1": ("resolved", "resolved"), "s2": ("unclear", "resolved")}
    adopt_run(db, home, cfg, model(judged), apsis)
    assert state(db) == (
        "waiting",
        f"{HELD}1 of the 3 sessions run showed the problem without the change; "
        "at least 2 are needed",
    )


def test_sessions_without_a_complete_opening_prompt_or_forgotten_are_not_replayed(
    db, home, repo
):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    when = days_ago(2)
    truncated = {"type": "text", "text": opening("s3"), "truncated": True}
    apsis = StubApsis(
        session("s1"),
        session("s2"),
        # The opening prompt was cut short in the record.
        session("s3", [{"role": "user", "content": [truncated], "id": "s3-u1"}]),
        # An exec stream records no prompt.
        session("s4", [make_entry("assistant", UNSOLVED, when, id="s4-a1")]),
        session("s5"),
    )
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s5", "s1", "s3", "s4", "s2"))
    knowledge.forget_session(db, "s5", "claude")
    found = model()

    adopt_run(db, home, cfg, found, apsis)
    assert state(db) == ("applied", None)
    assert ("claude", "s5") not in apsis.opened
    assert {d["opening_prompt"] for d in found.of("replayer")} == {
        opening("s1"),
        opening("s2"),
    }
    assert [r["session_id"] for r in replays(db)] == ["s1", "s2"]
    assert all("e-s5" not in str(d) for d in found.of("reviewer"))


def test_a_session_forgotten_during_the_replay_is_not_replayed_further(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))

    def forget_s3(role, data):
        if role == "reviewer" and data["evidence"] == ["quote e-s1"]:
            knowledge.forget_session(db, "s3", "claude")

    found = model(before=forget_s3)
    adopt_run(db, home, cfg, found, apsis)
    assert opening("s3") not in {d["opening_prompt"] for d in found.of("replayer")}
    assert [r["session_id"] for r in replays(db)] == ["s1", "s2"]


def test_too_few_replayable_sessions_hold_the_change_without_a_call(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(session("s1"), session("s2", []))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2"))
    found = model()
    adopt_run(db, home, cfg, found, apsis)
    assert state(db) == (
        "waiting",
        f"{HELD}only 1 session showing the pattern can be replayed; "
        "at least 2 are needed",
    )
    assert found.inputs == []
    assert replays(db) == []


def test_a_replay_the_budget_cannot_cover_holds_the_change(db, home, repo):
    cfg = configure({"agentic": repo}, budget={"tokens": 500})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    found = model()
    adopt_run(db, home, cfg, found, apsis)
    status, reason = state(db)
    assert status == "waiting"
    assert reason.startswith(
        f"{HELD}the remaining budget, 500 tokens, cannot cover the replay"
    )
    assert found.inputs == []


def test_replays_on_more_than_one_backend_hold_the_change(db, home, repo):
    cfg = config.build_config(
        {
            "backends": {
                name: {"harness": "claude", "context_tokens": 100_000}
                for name in ("fake", "spare")
            },
            "roles": {
                **{role: {"backends": ["fake"]} for role in config.ROLES},
                "replayer": {"backends": ["fake", "spare"]},
                "reviewer": {"backends": ["spare"]},
            },
            "repositories": {"agentic": {"path": str(repo), "test": ["sh check.sh"]}},
            "adoption": {"auto_apply": True},
        }
    )
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    # The first backend fails from the second session on, so the rest of the
    # replay falls back to the spare.
    first = model()
    answer = first.answer

    def failing_after_two(role, data):
        if len(first.inputs) > 2:
            raise BackendError("connection reset")
        return answer(role, data)

    first.answer = failing_after_two
    spare = model()
    spare.name = "spare"
    runner = run.Runner(
        db,
        home,
        RUN,
        cfg,
        phases={
            "adopt": adopt.AdoptPhase(
                replay.ReplayGate(roles.load(INSTRUCTIONS, cfg), INSTRUCTIONS, apsis)
            )
        },
        backends={"fake": first, "spare": spare},
        clock=FakeClock(),
        planner=lambda ctx: [NewStep("adopt", "adopt")],
    )
    runner.execute()
    assert state(db) == (
        "waiting",
        f"{HELD}its replays did not all run on one backend",
    )
    assert [r["backend"] for r in replays(db)] == ["fake", "spare", "spare"]


def test_a_change_is_replayed_once_per_authoring(db, home, repo, monkeypatch):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"))
    check = adopt.AdoptPhase._check
    crashes = iter([True])

    def crash_once(self, ctx, repo):
        if next(crashes, False):
            raise Crash
        return check(self, ctx, repo)

    monkeypatch.setattr(adopt.AdoptPhase, "_check", crash_once)
    with pytest.raises(Crash):
        adopt_run(db, home, cfg, model(), apsis)

    # The resumed Adopt step uses the answers already received.
    again = model()
    assert adopt_run(db, home, cfg, again, apsis) == "complete"
    assert again.inputs == []
    assert len(replays(db)) == 3
    assert state(db) == ("applied", None)


# Lenses and role prompts, re-run on stored inputs (9.16)


@pytest.fixture
def instruction_repo(tmp_path):
    found = make_repo(tmp_path, "dreaming", {"check.sh": "exit 0\n"})
    shutil.copytree(INSTRUCTIONS, found / "instructions")
    commit(found, {}, "instructions")
    return found


LOOK_FOR = "Explicit preferences and corrections:"
CHANGED_LOOK_FOR = "Explicit preferences, corrections and claims of success:"


def earlier_dream(db, home, cfg, apsis):
    """A dream that read the sessions under the run's lenses; its Read steps keep
    the inputs a reader replay re-runs."""
    found = ScriptedModel(answering())
    assert dream(db, home, cfg, apsis, found, run_id="dream-old") == "complete"
    rows = db.execute("SELECT evidence_id, pattern_id FROM observations ORDER BY 1")
    evidence, patterns = zip(*rows, strict=True)
    assert len(evidence) == 3 and len(set(patterns)) == 1
    return list(evidence), patterns[0]


def lens_sessions():
    when = days_ago(2)
    return StubApsis(
        *(
            make_doc(
                "claude",
                sid,
                when,
                entries=[
                    make_entry(
                        "user", "say done only after the tests", when, id=f"{sid}-u1"
                    ),
                    make_entry("assistant", UNSOLVED, when, id=f"{sid}-a1"),
                ],
            )
            for sid in ("s1", "s2", "s3")
        )
    )


def test_a_lens_change_re_runs_the_reader_on_its_stored_inputs(
    db, home, instruction_repo
):
    cfg = configure(
        {"dreaming": instruction_repo}, instructions={"repo": str(instruction_repo)}
    )
    apsis = lens_sessions()
    evidence, pattern = earlier_dream(db, home, cfg, apsis)
    observed = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    add_run(db, RUN, cfg, started_at=NOW)
    lenses = (instruction_repo / "instructions" / "lenses.toml").read_text()
    assert LOOK_FOR in lenses
    changed = lenses.replace(LOOK_FOR, CHANGED_LOOK_FOR)
    propose(
        db,
        home,
        cfg,
        {"instructions/lenses.toml": changed},
        evidence=evidence,
        repo="dreaming",
        pattern=pattern,
    )

    def answer(role, data):
        if role == "reader":
            return {"observations": []}
        if role == "reviewer":
            return {"without": "recurs", "with": "resolved", "reason": "as shown"}
        raise AssertionError(f"unexpected {role} call")

    found = ScriptedModel(answer)
    root = instruction_repo / "instructions"
    assert adopt_run(db, home, cfg, found, apsis, root=root) == "complete"
    assert state(db) == ("applied", None)
    read = found.of("reader")
    assert len(read) == 6
    looked_for = [
        next(lens["look_for"] for lens in d["lenses"] if lens["id"] == "preferences")
        for d in read
    ]
    assert all(text.startswith(LOOK_FOR) for text in looked_for[::2])
    assert all(text.startswith(CHANGED_LOOK_FOR) for text in looked_for[1::2])
    assert {d["entries"][0]["text"] for d in read} == {"say done only after the tests"}
    assert len(replays(db)) == 3
    # A replay's answers are compared, never stored as observations.
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == observed


def test_a_role_prompt_change_whose_inputs_are_not_kept_is_held(
    db, home, instruction_repo
):
    cfg = configure(
        {"dreaming": instruction_repo}, instructions={"repo": str(instruction_repo)}
    )
    add_run(db, RUN, cfg, started_at=NOW)
    prompt = (instruction_repo / "instructions" / "roles" / "reviewer.md").read_text()
    propose(
        db,
        home,
        cfg,
        {"instructions/roles/reviewer.md": f"{prompt}\nPrefer fewer candidates.\n"},
        evidence=seed(db, "s1", "s2"),
        repo="dreaming",
    )
    found = model()
    adopt_run(db, home, cfg, found, StubApsis(session("s1"), session("s2")))
    assert state(db) == (
        "waiting",
        f"{HELD}the reviewer role's earlier inputs are not kept, so a change to its "
        "prompt cannot be replayed",
    )
    assert found.inputs == []


def test_instruction_paths_in_another_repository_are_not_lenses(
    db, home, repo, tmp_path
):
    elsewhere = make_repo(tmp_path, "instructions-home", {"README.md": "x\n"})
    cfg = configure({"agentic": repo}, instructions={"repo": str(elsewhere)})
    add_run(db, RUN, cfg, started_at=NOW)
    propose(
        db,
        home,
        cfg,
        {"instructions/lenses.toml": "not a lens file\n"},
        evidence=seed(db, "s1", "s2"),
    )
    found = model()
    adopt_run(db, home, cfg, found, StubApsis(session("s1"), session("s2")))
    assert state(db) == ("applied", None)
    assert found.inputs == []


# Changes that are not replayed or never merged (9.16, 9.19)


def test_a_change_citing_only_file_findings_is_not_replayed(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    seed(db, "s1", "s2")
    propose(db, home, cfg, {RULE: FIX}, evidence=["ff-broken-link"])
    found = model()
    adopt_run(db, home, cfg, found, StubApsis(session("s1"), session("s2")))
    assert state(db) == ("applied", None)
    assert found.inputs == []
    assert replays(db) == []


def test_a_change_that_received_fetched_content_never_enters_the_merge(db, home, repo):
    cfg = configure({"agentic": repo})
    add_run(db, RUN, cfg, started_at=NOW)
    apsis = StubApsis(*(session(s) for s in ("s1", "s2", "s3")))
    propose(db, home, cfg, {RULE: FIX}, evidence=seed(db, "s1", "s2", "s3"), external=1)
    before = head(repo)
    found = model()
    adopt_run(db, home, cfg, found, apsis)
    assert state(db) == (
        "waiting",
        "change ch-c1 received fetched web or pull-request content (9.19)",
    )
    assert found.inputs == []
    assert not checked(db)
    assert head(repo) == before

"""The Research and Author steps with a fake model (4.2, 4.6, 9.2, 9.3, 9.10,
9.19, 11.5, 13.6, 15.1, 15.2)."""

import json
import subprocess

import pytest
from dreamkit import EMPTY_REVIEW, ScriptedModel, build_config, instructions
from runkit import FakeClock, add_run, git, make_runner, step_states
from sessiondocs import add_observation

from dreaming import adopt, author, knowledge, research, review
from dreaming.errors import Rejected
from dreaming.steps import NewStep

RUN = "dream-now"
NOW = "2026-11-15T09:00:00+00:00"
PYTHON_RULE = "# Python\n\n## Tooling\n\n- Use ruff for linting.\n"
SUMMARY = "Prefer uv in the Python rule"
NO_SCOPE = {
    "domains": [],
    "projects": [],
    "workflows": [],
    "global": False,
    "unresolved": False,
}


def make_repo(root, name):
    repo = root / name
    repo.mkdir()
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "CLAUDE.md").write_text("Write British English.\n")
    (repo / "AGENTS.md").write_text("Run make check before committing.\n")
    (repo / "rules").mkdir()
    (repo / "rules" / "python.md").write_text(PYTHON_RULE)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def head(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    return {name: make_repo(tmp_path, name) for name in ("agentic", "orbit")}


@pytest.fixture
def notes(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    return folder


def configure(repos, tmp_path, notes, *, public=(), context_tokens=100_000, **sections):
    return build_config(
        context_tokens,
        repositories={
            name: {"path": str(path), "public": name in public}
            for name, path in repos.items()
        },
        thresholds={"min_sessions": 3},
        knowledge_folders=[str(notes), str(tmp_path / "missing-notes")],
        **sections,
    )


@pytest.fixture
def cfg(repos, tmp_path, notes):
    return configure(repos, tmp_path, notes)


@pytest.fixture
def run_id(db, cfg):
    return add_run(db, RUN, cfg, started_at=NOW)


# Seeding the store


def pattern(db, pattern_id="p-uv", key="prefers uv", lens="preferences"):
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at) VALUES (?, ?, ?, ?)",
        (pattern_id, key, lens, NOW),
    )


def observe(db, eid, session, *, pattern_id="p-uv", **fields):
    db.execute(
        "INSERT OR IGNORE INTO sessions_seen (source, session_id, state)"
        " VALUES ('claude', ?, 'read')",
        (session,),
    )
    fields.setdefault("kind", "tool_result")
    fields.setdefault("lens", "preferences")
    fields.setdefault("project", "/work/repo")
    fields.setdefault("observed_on", "2026-11-10")
    add_observation(db, eid, "claude", session, pattern_id=pattern_id, **fields)
    return eid


def spread(db, prefix, count, **fields):
    return [
        observe(db, f"{prefix}{i}", f"{prefix}-s{i}", **fields) for i in range(count)
    ]


def file_finding(db, eid="ff1", *, path="rules/python.md", repo="agentic"):
    db.execute(
        "INSERT INTO file_findings (evidence_id, repo, revision, path, location,"
        " finding, lens, lens_version, pattern_id)"
        " VALUES (?, ?, 'abc1234', ?, 'L3', 'links a missing file',"
        " 'broken-references', 1, 'p-uv')",
        (eid, repo, path),
    )
    return eid


def candidate(
    db, cid, evidence, *, proposal="change", repo="agentic", pattern_id="p-uv"
):
    db.execute(
        "INSERT INTO candidates (id, run, pattern_id, proposal, repo, summary,"
        " evidence, position, signals, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'recurs', ?)",
        (
            cid,
            RUN,
            pattern_id,
            proposal,
            repo if proposal == "change" else None,
            SUMMARY,
            json.dumps(sorted(evidence)),
            json.dumps({"sessions": 3, "score": 3.5}),
            NOW,
        ),
    )
    return cid


def question(db, qid="q1", cid="c1", text="Which rule covers uv?"):
    db.execute(
        "INSERT INTO questions (id, run, candidate_id, question, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (qid, RUN, cid, text, NOW),
    )
    return qid


def change_candidate(db, cid="c1", **fields):
    """A change candidate on p-uv resting on three sessions: u0, u1, u2."""
    pattern(db)
    ids = spread(db, "u", 3)
    candidate(db, cid, ids, **fields)
    return ids


def contested_item(db, contradicting):
    """An item on p-uv whose evidence conflicts, which gives the author counter
    cases."""
    db.execute(
        "INSERT INTO knowledge_items (id, kind, status, pattern_id, created_at,"
        " updated_at) VALUES ('k-uv', 'preference', 'contested', 'p-uv', ?, ?)",
        (NOW, NOW),
    )
    db.execute(
        "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
        " confidence, supporting, contradicting, actor, created_at)"
        " VALUES ('k-uv', 1, 'prefers uv', 'preference', 'contested', 'low',"
        " '[\"u0\"]', ?, 'dream', ?)",
        (json.dumps(contradicting), NOW),
    )


# Answers


def source(kind="web", link="https://docs.astral.sh/uv/", passage="uv is fast"):
    return {
        "title": "uv",
        "link": link,
        "retrieved_on": "2026-11-15",
        "passage": passage,
        "kind": kind,
    }


def finding(claim, *sources):
    return {
        "claim": claim,
        "assessment": "answers",
        "equivalent_finding": None,
        "sources": list(sources),
    }


def edit(**fields):
    return {
        "path": "rules/python.md",
        "section": "## Tooling",
        "action": "add",
        "text": "- Use uv, not pip.",
        "supporting": ["u0"],
        "counter": [],
        "applies_when": "Python projects managed with uv",
        **fields,
    }


def change(evidence=("u0", "u1", "u2"), *, edits=None, repo="agentic", depends_on=()):
    return {
        "repo": repo,
        "summary": "Prefer uv",
        "commit_message": f"{SUMMARY}\n\nThree sessions reached for uv.",
        "depends_on": list(depends_on),
        "evidence": list(evidence),
        "edits": edits or [edit()],
    }


def recommendation(evidence, research_ids=(), scope=NO_SCOPE):
    return {
        "summary": "Python packaging with uv",
        "next_step": "Read the uv guide on projects",
        "helps_achieve": "Fewer questions about lock files",
        "scope": scope,
        "evidence": list(evidence),
        "research": list(research_ids),
    }


def authored(*changes, learning=(), outcome=None, supported=True, deferred=()):
    if outcome is None:
        outcome = "change" if changes else "learning" if learning else "defer"
    return {
        "verification": {
            "supported": supported,
            "reason": "still applies" if supported else "the rule says uv already",
        },
        "outcome": outcome,
        "changes": list(changes),
        "learning": list(learning),
        "deferred_steps": list(deferred),
    }


NO_FINDINGS = {"findings": [], "unavailable": []}


def model(author_answer=None, research_answer=NO_FINDINGS):
    def answer(role, data):
        if role == "researcher":
            return research_answer
        if role == "author":
            return author_answer(data) if callable(author_answer) else author_answer
        raise AssertionError(f"unexpected {role} call")

    return ScriptedModel(answer)


def run_steps(db, cfg, fake, planned, home):
    found = instructions(cfg)
    runner = make_runner(
        db,
        home,
        RUN,
        cfg,
        phases={
            "research": research.ResearchPhase(found),
            "author": author.AuthorPhase(found),
        },
        model=fake,
        clock=FakeClock(),
        planner=lambda ctx: planned,
    )
    return runner.execute()


def author_step(cid="c1", after=()):
    return NewStep(f"author:{cid}", "author", depends_on=tuple(after), input_ref=cid)


def research_step(qid="q1"):
    return NewStep(f"research:{qid}", "research", input_ref=qid)


def candidate_row(db, cid="c1"):
    return db.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()


def changes(db):
    return db.execute("SELECT * FROM changes ORDER BY id").fetchall()


def rejections(db, what):
    rows = db.execute("SELECT * FROM rejections WHERE what = ? ORDER BY id", (what,))
    return [row["reason"] for row in rows]


# Research (4.6, 13.6, 15.2)


def test_research_findings_keep_their_sources_apart_from_observations(
    db, cfg, home, run_id, repos, notes, tmp_path
):
    change_candidate(db)
    question(db)
    local = source("repository", "agentic:rules/python.md@abc1234", "Use ruff")
    answer = {
        "findings": [
            finding("uv replaces pip", source()),
            finding("the rule names ruff", local),
            finding("an unsourced claim"),
        ],
        "unavailable": [{"source": "https://gone.example", "reason": "404"}],
    }
    fake = model(research_answer=answer)
    assert run_steps(db, cfg, fake, [research_step()], home) == "complete"

    (data,) = fake.of("researcher")
    assert data["question"] == "Which rule covers uv?"
    assert data["candidate"] == {
        "id": "c1",
        "proposal": "change",
        "summary": SUMMARY,
        "repo": "agentic",
    }
    assert data["repositories"] == [
        {"name": name, "path": str(path), "default_branch": "main"}
        for name, path in repos.items()
    ]
    assert data["knowledge_folders"] == [str(notes)]

    rows = db.execute("SELECT * FROM research_findings ORDER BY finding").fetchall()
    assert [
        (r["finding"], r["assessment"], r["fetched"], r["question_id"], r["backend"])
        for r in rows
    ] == [
        ("the rule names ruff", "answers", 0, "q1", "fake"),
        ("uv replaces pip", "answers", 1, "q1", "fake"),
    ]
    assert json.loads(rows[1]["sources"]) == [source()]
    assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
    assert rejections(db, "finding") == ["the finding cites no source"]
    (unavailable,) = db.execute("SELECT unavailable FROM questions")
    assert json.loads(unavailable[0]) == [
        {
            "source": str(tmp_path / "missing-notes"),
            "reason": "the knowledge folder does not exist",
        },
        {"source": "https://gone.example", "reason": "404"},
    ]
    # A finding is never evidence about the user.
    with pytest.raises(Rejected):
        knowledge.check_evidence(db, [rows[0]["id"]])
    (ref,) = db.execute("SELECT input_ref FROM calls WHERE role = 'researcher'")
    assert json.loads(ref[0])["sessions"] == [
        ["claude", "u-s0"],
        ["claude", "u-s1"],
        ["claude", "u-s2"],
    ]


@pytest.mark.parametrize(
    "kind, link, fetched",
    [
        ("web", "https://docs.astral.sh/uv/", True),
        ("pull_request", "https://github.com/o/r/pull/1", True),
        ("repository", "https://example.com/looks-local", True),
        ("knowledge_folder", "www.example.com/notes", True),
        ("repository", "agentic:rules/python.md@abc1234", False),
        ("knowledge_folder", "~/notes/uv.md", False),
    ],
)
def test_only_local_paths_count_as_not_fetched(kind, link, fetched):
    assert research.fetched(source(kind, link)) is fetched


# What the author sees (4.3, 11.5)


def test_the_author_gets_evidence_research_files_and_instruction_files(
    db, cfg, home, run_id, repos
):
    pattern(db)
    ids = spread(db, "u", 3)
    candidate(db, "c1", [*ids, file_finding(db)])
    observe(db, "x0", "x-s0", kind="user_statement")
    contested_item(db, ["x0"])
    question(db)
    # The user's uncommitted edit is not what the author sees.
    (repos["agentic"] / "CLAUDE.md").write_text("an uncommitted edit\n")
    fake = model(
        authored(change()),
        {"findings": [finding("uv replaces pip", source())], "unavailable": []},
    )
    run_steps(
        db, cfg, fake, [research_step(), author_step(after=["research:q1"])], home
    )

    (data,) = fake.of("author")
    assert data["candidate"] == {
        "id": "c1",
        "proposal": "change",
        "summary": SUMMARY,
        "reason": "recurs",
        "repo": "agentic",
    }
    by_id = {e["id"]: e for e in data["evidence"]}
    assert sorted(by_id) == ["ff1", "u0", "u1", "u2"]
    assert by_id["u0"]["quote"] == "quote u0"
    assert by_id["u0"]["modality"] == "asserted"
    assert by_id["u0"]["availability"] == "available"
    assert by_id["ff1"] == {
        "id": "ff1",
        "repo": "agentic",
        "revision": "abc1234",
        "path": "rules/python.md",
        "location": "L3",
        "finding": "links a missing file",
        "availability": "available",
    }
    assert [e["id"] for e in data["counter_evidence"]] == ["x0"]
    assert [f["claim"] for f in data["research"]] == ["uv replaces pip"]
    assert data["research"][0]["sources"] == [source()]
    assert data["repository"] == {
        "name": "agentic",
        "default_branch": "main",
        "revision": head(repos["agentic"]),
        "public": False,
        "work_copy": str(home / "work" / RUN / "agentic"),
    }
    assert data["instruction_files"] == {
        "AGENTS.md": "Run make check before committing.\n",
        "CLAUDE.md": "Write British English.\n",
    }
    assert data["files"] == {"rules/python.md": PYTHON_RULE}
    assert data["omitted_files"] == []


def test_files_that_do_not_fit_are_left_for_the_author_to_read(
    db, repos, tmp_path, notes, home
):
    big = "## Tooling\n" + "- a rule that goes on and on\n" * 2000
    (repos["agentic"] / "rules" / "python.md").write_text(big)
    git(repos["agentic"], "commit", "-q", "-am", "grow")
    cfg = configure(repos, tmp_path, notes, context_tokens=8000)
    add_run(db, RUN, cfg, started_at=NOW)
    pattern(db)
    candidate(db, "c1", [*spread(db, "u", 3), file_finding(db)])
    fake = model(authored(change()))
    run_steps(db, cfg, fake, [author_step()], home)
    (data,) = fake.of("author")
    assert data["files"] == {}
    assert data["omitted_files"] == ["rules/python.md"]
    assert data["instruction_files"]["CLAUDE.md"] == "Write British English.\n"


# Program-side verification before authoring (11.5)


def test_the_author_step_defers_when_cited_evidence_no_longer_holds(
    db, cfg, home, run_id
):
    change_candidate(db)
    knowledge.forget_session(db, "u-s0")
    fake = model(authored(change()))
    run_steps(db, cfg, fake, [author_step()], home)
    assert fake.of("author") == []
    row = candidate_row(db)
    assert row["status"] == "deferred"
    assert "forgotten" in row["reason"]
    assert changes(db) == []
    assert step_states(db, RUN) == {"author:c1": "done"}


def test_the_author_step_defers_when_a_cited_file_is_gone(db, cfg, home, run_id, repos):
    pattern(db)
    candidate(db, "c1", [*spread(db, "u", 3), file_finding(db)])
    git(repos["agentic"], "rm", "-q", "rules/python.md")
    git(repos["agentic"], "commit", "-q", "-m", "remove the rule")
    fake = model(authored(change()))
    run_steps(db, cfg, fake, [author_step()], home)
    assert fake.of("author") == []
    row = candidate_row(db)
    assert row["status"] == "deferred"
    assert row["reason"] == "rules/python.md no longer exists in agentic"


def test_the_author_can_defer_after_checking_the_current_files(db, cfg, home, run_id):
    change_candidate(db)
    run_steps(db, cfg, model(authored(supported=False)), [author_step()], home)
    row = candidate_row(db)
    assert (row["status"], row["reason"]) == ("deferred", "the rule says uv already")
    assert changes(db) == []


# Changes (9.2, 9.10, 9.19)


@pytest.mark.parametrize("public", [False, True])
def test_an_authored_change_is_stored_with_its_edits_and_cited_evidence(
    db, home, repos, tmp_path, notes, public
):
    cfg = configure(repos, tmp_path, notes, public=["agentic"] if public else [])
    add_run(db, RUN, cfg, started_at=NOW)
    pattern(db)
    ids = spread(db, "u", 3)
    candidate(db, "c1", [*ids, file_finding(db)])
    edits = [edit(supporting=["u2"], counter=["u1"])]
    run_steps(
        db,
        cfg,
        model(authored(change(["u0", "ff1"], edits=edits))),
        [author_step()],
        home,
    )
    (row,) = changes(db)
    assert (row["status"], row["candidate_id"], row["repo"], row["pattern_id"]) == (
        "proposed",
        "c1",
        "agentic",
        "p-uv",
    )
    assert json.loads(row["evidence"]) == ["ff1", "u0", "u2"]
    assert json.loads(row["edits"]) == edits
    assert row["base_revision"] == head(repos["agentic"])
    assert row["external_input"] == 0
    assert json.loads(row["rank"]) == {"position": 1, "sessions": 3, "score": 3.5}
    lines = row["commit_message"].splitlines()
    assert lines[:4] == [SUMMARY, "", "Three sessions reached for uv.", ""]
    cited = "ff1" if public else "ff1 (agentic abc1234 rules/python.md L3)"
    assert lines[4:] == [
        f"Dreaming-Evidence: {cited}",
        "Dreaming-Evidence: u0",
        "Dreaming-Evidence: u2",
    ]
    assert candidate_row(db)["status"] == "authored"


def test_a_new_file_may_be_added_whole(db, cfg, home, run_id):
    change_candidate(db)
    new = edit(path="rules/uv.md", section="# uv", text="# uv\n\nUse uv.\n")
    run_steps(db, cfg, model(authored(change(edits=[new]))), [author_step()], home)
    (row,) = changes(db)
    assert json.loads(row["edits"]) == [new]


@pytest.mark.parametrize(
    "bad, reason",
    [
        (edit(section="rules/python.md"), "names the whole file"),
        (edit(section="python.md"), "names the whole file"),
        (edit(section="## Packaging"), "is not in rules/python.md"),
        (edit(path="rules/new.md", action="replace"), "rules/new.md does not exist"),
        (edit(path="../outside.md"), "is not a path inside the repository"),
        (edit(path="/etc/hosts"), "is not a path inside the repository"),
        (edit(path=".git/config"), "is not a path inside the repository"),
        (edit(action="replace", text=None), "needs text"),
        (edit(action="delete", text="x"), "a delete takes no text"),
        (edit(supporting=[]), "cites no supporting case"),
        (edit(supporting=["ev-elsewhere"]), "were not given: ev-elsewhere"),
        (edit(counter=["ev-unknown"]), "were not given: ev-unknown"),
    ],
)
def test_edits_are_keyed_to_sections_and_cite_a_supporting_case(
    db, cfg, home, run_id, bad, reason
):
    change_candidate(db)
    run_steps(
        db, cfg, model(authored(change(edits=[edit(), bad]))), [author_step()], home
    )
    assert changes(db) == []
    (rejected,) = rejections(db, "author")
    assert reason in rejected
    row = candidate_row(db)
    assert row["status"] == "discarded"
    assert reason in row["reason"]


@pytest.mark.parametrize(
    "answer, reason",
    [
        (
            authored(learning=[recommendation(["u0"])]),
            "a change candidate was authored as learning",
        ),
        (authored(change(repo="orbit")), "the change is for orbit, not agentic"),
        (authored(change(), change()), "exactly one change"),
        (authored(change([])), "cites no evidence"),
        (
            authored(change(depends_on=["elsewhere"])),
            "depends on elsewhere, which is not a configured repository",
        ),
        (authored(change(depends_on=["agentic"])), "depends on its own repository"),
        (
            authored({**change(), "commit_message": "  \n\nbody"}),
            "the commit message has no subject",
        ),
    ],
)
def test_changes_that_break_the_rules_are_discarded_with_the_reason(
    db, cfg, home, run_id, answer, reason
):
    change_candidate(db)
    run_steps(db, cfg, model(answer), [author_step()], home)
    assert changes(db) == []
    assert reason in rejections(db, "author")[0]


def test_depends_on_names_the_repositories_a_change_needs(db, cfg, home, run_id):
    change_candidate(db)
    answer = authored(change(depends_on=["orbit", "orbit"]))
    run_steps(db, cfg, model(answer), [author_step()], home)
    (row,) = changes(db)
    assert json.loads(row["depends_on"]) == ["orbit"]


def test_depends_on_defines_linked_groups(db, cfg, run_id):
    def add(change_id, repo, depends_on=()):
        db.execute(
            "INSERT INTO changes (id, run, repo, depends_on, status, created_at)"
            " VALUES (?, ?, ?, ?, 'proposed', ?)",
            (change_id, RUN, repo, json.dumps(list(depends_on)), NOW),
        )

    add("ch-1", "agentic", ["orbit"])
    add("ch-2", "screw", ["apsis"])
    add("ch-3", "apsis", ["orbit"])
    add("ch-4", "notes")
    add("ch-5", "agentic")
    assert adopt.linked_groups(db, RUN) == [
        ["agentic", "apsis", "orbit", "screw"],
        ["notes"],
    ]


@pytest.mark.parametrize(
    "found, author_tools, external",
    [
        ([finding("uv replaces pip", source())], None, 1),
        ([finding("see the PR", source("pull_request", "https://gh/o/r/1"))], None, 1),
        (
            [finding("the rule names ruff", source("repository", "agentic:x@1"))],
            None,
            0,
        ),
        ([], None, 0),
        ([], ["read", "edit", "devtools.fetch_url"], 1),
    ],
)
def test_external_input_marks_changes_that_received_fetched_text(
    db, home, repos, tmp_path, notes, found, author_tools, external
):
    tools = {"tools": {"author": author_tools}} if author_tools else {}
    cfg = configure(repos, tmp_path, notes, **tools)
    add_run(db, RUN, cfg, started_at=NOW)
    change_candidate(db)
    question(db)
    fake = model(authored(change()), {"findings": found, "unavailable": []})
    run_steps(
        db, cfg, fake, [research_step(), author_step(after=["research:q1"])], home
    )
    (row,) = changes(db)
    assert row["external_input"] == external
    research_ids = [r[0] for r in db.execute("SELECT id FROM research_findings")]
    assert json.loads(row["research"]) == research_ids


def test_interactive_skill_steps_are_deferred_and_reported(db, cfg, home, run_id):
    change_candidate(db)
    step = {
        "skill": "release-prep",
        "step": "bump the version",
        "reason": "asks which version to release",
    }
    run_steps(
        db, cfg, model(authored(change(), deferred=[step])), [author_step()], home
    )
    assert json.loads(candidate_row(db)["deferred_steps"]) == [step]
    assert changes(db)[0]["status"] == "proposed"


# Learning recommendations (15.1, 15.2)


def learning_candidate(db):
    pattern(db, "p-help", "asks about uv", lens="help-requests")
    ids = spread(
        db,
        "h",
        3,
        pattern_id="p-help",
        lens="help-requests",
        kind="user_statement",
        domain="software",
    )
    candidate(db, "c1", ids, proposal="learning", pattern_id="p-help")
    return ids


def test_a_learning_candidate_becomes_a_recommendation(db, cfg, home, run_id):
    ids = learning_candidate(db)
    question(db, text="What is a good uv guide?")
    found = {"findings": [finding("the uv guide", source())], "unavailable": []}

    def answer(data):
        return authored(
            learning=[recommendation(ids, [f["id"] for f in data["research"]])]
        )

    planned = [research_step(), author_step(after=["research:q1"])]
    run_steps(db, cfg, model(answer, found), planned, home)
    (finding_id,) = [r[0] for r in db.execute("SELECT id FROM research_findings")]
    (row,) = db.execute("SELECT * FROM learning")
    assert (row["summary"], row["next_step"], row["outcome"]) == (
        "Python packaging with uv",
        "Read the uv guide on projects",
        "Fewer questions about lock files",
    )
    assert json.loads(row["evidence"]) == ids
    assert json.loads(row["research"]) == [finding_id]
    assert json.loads(row["scope"])["projects"] == ["/work/repo"]
    assert (row["disposition"], row["pattern_id"], row["run"]) == (
        "open",
        "p-help",
        RUN,
    )
    assert changes(db) == []
    assert candidate_row(db)["status"] == "authored"


@pytest.mark.parametrize(
    "evidence, research_ids, reason",
    [
        (["h0", "h1", "h2", "rf-made-up"], [], "were not given: rf-made-up"),
        (["h0"], ["ev-unknown"], "research findings that were not given"),
        ([], [], "cites no evidence"),
    ],
)
def test_learning_keeps_research_apart_from_evidence(
    db, cfg, home, run_id, evidence, research_ids, reason
):
    learning_candidate(db)
    answer = authored(learning=[recommendation(evidence, research_ids)])
    run_steps(db, cfg, model(answer), [author_step()], home)
    assert db.execute("SELECT COUNT(*) FROM learning").fetchone()[0] == 0
    assert reason in rejections(db, "author")[0]


# From review to authoring


def test_a_review_candidate_is_researched_then_authored(db, cfg, home, run_id):
    pattern(db)
    ids = spread(db, "u", 3)
    spread(db, "n", 1, pattern_id=None)
    db.execute("UPDATE observations SET pattern_key = 'new' WHERE evidence_id = 'n0'")

    def answer(role, data):
        if role == "reviewer":
            return {
                **EMPTY_REVIEW,
                "candidates": [
                    {
                        "pattern": "p-uv",
                        "proposal": "change",
                        "repo": "agentic",
                        "summary": SUMMARY,
                        "evidence": ids,
                        "reason": "recurs",
                    }
                ],
                "questions": [
                    {"question": "Which rule covers uv?", "candidate": 0, "reason": ""}
                ],
            }
        if role == "researcher":
            return {
                "findings": [finding("uv replaces pip", source())],
                "unavailable": [],
            }
        assert role == "author"
        assert [f["claim"] for f in data["research"]] == ["uv replaces pip"]
        return authored(change())

    found = instructions(cfg)
    runner = make_runner(
        db,
        home,
        RUN,
        cfg,
        phases={
            "review": review.ReviewPhase(found),
            "research": research.ResearchPhase(found),
            "author": author.AuthorPhase(found),
            "adopt": adopt.AdoptPhase(),
        },
        model=ScriptedModel(answer),
        clock=FakeClock(),
        planner=lambda ctx: [NewStep("review", "review")],
    )
    assert runner.execute() == "complete"
    (row,) = changes(db)
    assert (row["status"], row["external_input"]) == ("waiting", 1)
    assert row["reason"] == "auto-apply is disabled"
    states = step_states(db, RUN)
    assert set(states.values()) == {"done"}
    assert {"adopt", "adopt:agentic"} <= set(states)

"""Dream branches, one commit per change and the public scan (9.1, 9.2, 9.4,
9.5, 9.12), through the Author step with a fake author that edits its work
copy."""

import json
from pathlib import Path

import pytest
from dreamkit import ScriptedModel, build_config, instructions, make_edits
from gitkit import branches, commit, head, make_repo, out, status
from runkit import Crash, FakeClock, add_run, git, make_runner
from sessiondocs import add_observation

from dreaming import adopt, author, config
from dreaming.steps import NewStep

RUN = "dream-now"
NOW = "2026-11-15T09:00:00+00:00"
BRANCH = adopt.branch_name(RUN)
RULE = "# Python\n\n## Tooling\n\n- Use ruff for linting.\n"
QUOTE = "always write the release notes before the version is bumped, never after"
SECRET = "ghp_" + "a1B2" * 9


@pytest.fixture
def repos(tmp_path):
    return {
        name: make_repo(tmp_path, name, {"rules/python.md": RULE})
        for name in ("agentic", "orbit")
    }


def configure(repos, tmp_path, *, names=(), **settings):
    names_file = tmp_path / "names.txt"
    names_file.write_text("".join(f"{name}\n" for name in names))
    return build_config(
        repositories={
            name: {"path": str(path), **settings.get(name, {})}
            for name, path in repos.items()
        },
        thresholds={"min_sessions": 3},
        scan={"names_file": str(names_file), "quote_chars": 40},
    )


def candidate(db, cid, repo="agentic", quote=None):
    """A change candidate resting on three sessions, its evidence quoting
    `quote` when given."""
    pattern = f"p-{cid}"
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at) VALUES (?, ?, 'prefs', ?)",
        (pattern, cid, NOW),
    )
    ids = []
    for i in range(3):
        eid, session = f"{cid}-e{i}", f"{cid}-s{i}"
        db.execute(
            "INSERT INTO sessions_seen (source, session_id, state)"
            " VALUES ('claude', ?, 'read')",
            (session,),
        )
        add_observation(
            db, eid, "claude", session, kind="tool_result", pattern_id=pattern
        )
        ids.append(eid)
    if quote:
        db.execute(
            "UPDATE observations SET quote = ? WHERE evidence_id = ?", (quote, ids[0])
        )
    db.execute(
        "INSERT INTO candidates (id, run, pattern_id, proposal, repo, summary,"
        " evidence, position, signals, created_at)"
        " VALUES (?, ?, ?, 'change', ?, ?, ?, 1, '{}', ?)",
        (cid, RUN, pattern, repo, f"Change {cid}", json.dumps(ids), NOW),
    )
    return ids


def adds(text, path="rules/python.md", section="## Tooling"):
    return {
        "path": path,
        "section": section,
        "action": "add",
        "text": text,
        "supporting": [],
        "counter": [],
        "applies_when": "always",
    }


def answer_with(edits=None, message=None):
    """An author answering each candidate with one change: the given edits, or
    by default a new rule file named after the candidate."""

    def answer(role, data):
        c = data["candidate"]
        evidence = [e["id"] for e in data["evidence"]]
        chosen = edits or [
            adds(f"# {c['id']}\n", f"rules/{c['id']}.md", f"# {c['id']}")
        ]
        return {
            "verification": {"supported": True, "reason": ""},
            "outcome": "change",
            "changes": [
                {
                    "repo": c["repo"],
                    "summary": c["summary"],
                    "commit_message": message or f"{c['summary']}\n\nBody.",
                    "depends_on": [],
                    "evidence": evidence,
                    "edits": [{**e, "supporting": evidence[:1]} for e in chosen],
                }
            ],
            "learning": [],
            "deferred_steps": [],
        }

    return answer


def run_author(db, home, cfg, cids, fake, phase=None):
    planned = [NewStep(f"author:{cid}", "author", input_ref=cid) for cid in cids]
    runner = make_runner(
        db,
        home,
        RUN,
        cfg,
        phases={"author": phase or author.AuthorPhase(instructions(cfg))},
        model=fake,
        clock=FakeClock(),
        planner=lambda ctx: planned,
    )
    return runner.execute()


def changes(db):
    return {
        row["candidate_id"]: row
        for row in db.execute("SELECT * FROM changes ORDER BY candidate_id")
    }


def on_branch(repo):
    """The dream branch's commits after the default branch, oldest first."""
    return out(repo, "rev-list", "--reverse", f"main..{BRANCH}").split()


def touched(repo, sha):
    return sorted(out(repo, "show", "--name-only", "--format=", sha).split())


def message(repo, sha):
    return out(repo, "log", "-1", "--format=%B", sha)


# The dream branch (9.1, 9.2)


def test_each_repository_gets_one_dream_branch_from_its_current_default_branch(
    db, home, repos, tmp_path
):
    cfg = configure(repos, tmp_path)
    add_run(db, RUN, cfg, started_at=NOW)
    moved = commit(repos["agentic"], {"README.md": "moved on\n"}, "user's work")
    ids = {
        cid: candidate(db, cid, repo)
        for cid, repo in [("c1", "agentic"), ("c2", "agentic"), ("c3", "orbit")]
    }
    assert (
        run_author(db, home, cfg, ["c1", "c2", "c3"], ScriptedModel(answer_with()))
        == "complete"
    )

    agentic, orbit = repos["agentic"], repos["orbit"]
    assert BRANCH in branches(agentic) and BRANCH in branches(orbit)
    assert out(agentic, "merge-base", "main", BRANCH) == moved
    rows = changes(db)
    # One commit per change, in authoring order, each holding only its change.
    assert on_branch(agentic) == [rows["c1"]["commit_sha"], rows["c2"]["commit_sha"]]
    assert on_branch(orbit) == [rows["c3"]["commit_sha"]]
    for cid, repo in [("c1", agentic), ("c2", agentic), ("c3", orbit)]:
        row = rows[cid]
        assert (row["status"], row["branch"]) == ("proposed", BRANCH)
        assert touched(repo, row["commit_sha"]) == [f"rules/{cid}.md"]
        # The commit cites the change's evidence and names the change.
        lines = message(repo, row["commit_sha"]).splitlines()
        assert [f"Dreaming-Evidence: {i}" for i in ids[cid]] == lines[-4:-1]
        assert lines[-1] == f"{adopt.CHANGE_TRAILER}: {row['id']}"
        assert message(repo, row["commit_sha"]).startswith(row["commit_message"])


def test_the_users_checkout_is_untouched(db, home, repos, tmp_path):
    cfg = configure(repos, tmp_path)
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    repo = repos["agentic"]
    (repo / "rules" / "python.md").write_text("an uncommitted edit\n")
    (repo / "scratch.txt").write_text("untracked\n")
    before = (status(repo), head(repo), head(repo, "main"))
    fake = ScriptedModel(answer_with([adds("- Use uv, not pip.")]))
    run_author(db, home, cfg, ["c1"], fake)

    assert (status(repo), head(repo), head(repo, "main")) == before
    assert (repo / "rules" / "python.md").read_text() == "an uncommitted edit\n"
    # The author wrote only its work copy, on top of the default branch.
    assert fake.workdirs == [home / "work" / RUN / "agentic"]
    (sha,) = on_branch(repo)
    assert out(repo, "show", f"{sha}:rules/python.md") == RULE.replace(
        "## Tooling", "## Tooling\n- Use uv, not pip."
    ).rstrip("\n")


def test_work_copies_are_removed_when_the_run_ends_and_branches_stay(
    db, home, repos, tmp_path
):
    cfg = configure(repos, tmp_path)
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    run_author(db, home, cfg, ["c1"], ScriptedModel(answer_with()))
    assert not (home / "work" / RUN).exists()
    assert out(repos["agentic"], "worktree", "list").count("\n") == 0
    assert len(on_branch(repos["agentic"])) == 1


@pytest.mark.parametrize(
    "edit, reason",
    [
        (lambda work, output: None, "the author made no edit in the work copy"),
        (
            lambda work, output: (
                make_edits(work, output),
                (Path(work) / "notes.md").write_text("undeclared\n"),
            ),
            "changed notes.md, rules/c1.md; declared rules/c1.md",
        ),
    ],
)
def test_the_author_may_write_only_the_edits_it_declares(
    db, home, repos, tmp_path, edit, reason
):
    cfg = configure(repos, tmp_path)
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    run_author(db, home, cfg, ["c1"], ScriptedModel(answer_with(), edit=edit))
    assert changes(db) == {}
    row = db.execute("SELECT status, reason FROM candidates").fetchone()
    assert row["status"] == "discarded"
    assert reason in row["reason"]
    assert on_branch(repos["agentic"]) == []


def test_a_change_committed_before_a_crash_is_committed_once(db, home, repos, tmp_path):
    cfg = configure(repos, tmp_path)
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    crashes = iter([True])

    class CrashInApply(author.AuthorPhase):
        def apply(self, conn, step, prepared):
            if next(crashes, False):
                raise Crash
            return super().apply(conn, step, prepared)

    fake = ScriptedModel(answer_with())
    phase = CrashInApply(instructions(cfg))
    with pytest.raises(Crash):
        run_author(db, home, cfg, ["c1"], fake, phase)
    (committed,) = on_branch(repos["agentic"])
    assert changes(db) == {}

    assert run_author(db, home, cfg, ["c1"], fake, phase) == "complete"
    assert len(fake.of("author")) == 1
    assert on_branch(repos["agentic"]) == [committed]
    assert changes(db)["c1"]["commit_sha"] == committed


# Generate commands (9.5)


def test_generate_commands_run_in_the_work_copy_before_the_commit(
    db, home, repos, tmp_path
):
    cfg = configure(
        repos, tmp_path, agentic={"generate": ["cat rules/*.md > rules/ALL.md"]}
    )
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    run_author(db, home, cfg, ["c1"], ScriptedModel(answer_with()))
    repo = repos["agentic"]
    (sha,) = on_branch(repo)
    assert touched(repo, sha) == ["rules/ALL.md", "rules/c1.md"]
    assert "# c1" in out(repo, "show", f"{sha}:rules/ALL.md")
    assert not (repo / "rules" / "ALL.md").exists()


def test_a_failing_generate_command_withholds_the_change(db, home, repos, tmp_path):
    cfg = configure(repos, tmp_path, agentic={"generate": ["echo broken >&2; exit 3"]})
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    run_author(db, home, cfg, ["c1"], ScriptedModel(answer_with()))
    row = changes(db)["c1"]
    assert row["status"] == "withheld"
    assert (
        row["reason"]
        == "the generate command 'echo broken >&2; exit 3' exited 3: broken"
    )
    assert "rules/c1.md" in Path(row["patch_path"]).read_text()
    assert on_branch(repos["agentic"]) == []


# The public scan (9.12)


@pytest.mark.parametrize(
    "text, commit_message, problem",
    [
        (f"- {QUOTE}", None, "transcript text from c1-e0 in rules/python.md"),
        (f"- token: {SECRET}", None, "a credential or token in rules/python.md"),
        ("- As Acme Corp asks.", None, "a name from the names list in rules/python.md"),
        (
            "- Use uv.",
            "Prefer uv\n\nAcme Corp asked for it.",
            "a name from the names list in the commit message",
        ),
    ],
)
def test_a_change_failing_the_public_scan_is_withheld_as_a_patch(
    db, home, repos, tmp_path, text, commit_message, problem
):
    cfg = configure(repos, tmp_path, names=["Acme Corp"], agentic={"public": True})
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1", quote=QUOTE)
    fake = ScriptedModel(answer_with([adds(text)], commit_message))
    run_author(db, home, cfg, ["c1"], fake)

    row = changes(db)["c1"]
    assert (row["status"], row["reason"], row["branch"]) == ("withheld", problem, None)
    assert on_branch(repos["agentic"]) == []
    patch = Path(row["patch_path"])
    assert patch == adopt.patch_path(home, RUN, row["id"])
    assert config.containing_repository(patch.parent) is None
    saved = patch.read_text()
    assert saved.startswith(row["commit_message"])
    assert f"+{text}" in saved
    # The saved patch applies to the default branch.
    adopt._git(repos["agentic"], "apply", "--check", str(patch))


def test_public_text_already_in_the_repository_is_committed(db, home, repos, tmp_path):
    commit(repos["agentic"], {"NOTES.md": f"{QUOTE}\n"})
    cfg = configure(repos, tmp_path, agentic={"public": True})
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1", quote=QUOTE)
    run_author(db, home, cfg, ["c1"], ScriptedModel(answer_with([adds(f"- {QUOTE}")])))
    assert changes(db)["c1"]["status"] == "proposed"


def test_a_private_repository_is_not_scanned(db, home, repos, tmp_path):
    cfg = configure(repos, tmp_path, names=["Acme Corp"])
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1", quote=QUOTE)
    fake = ScriptedModel(answer_with([adds(f"- Acme Corp: {QUOTE}")]))
    run_author(db, home, cfg, ["c1"], fake)
    assert changes(db)["c1"]["status"] == "proposed"


def test_no_branch_is_started_where_an_earlier_merge_was_not_deployed(
    db, home, repos, tmp_path
):
    cfg = configure(repos, tmp_path)
    add_run(db, "dream-old", cfg, status="complete", started_at=NOW)
    repo = repos["agentic"]
    git(repo, "checkout", "-q", "-b", "dream/dream-old")
    commit(repo, {"old.md": "x\n"}, "old change")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "old merge", "dream/dream-old")
    db.execute(
        "INSERT INTO adoption (run, repo, branch, step, head_after, at)"
        " VALUES ('dream-old', 'agentic', 'dream/dream-old', 'merging', ?, ?)",
        (head(repo), NOW),
    )
    add_run(db, RUN, cfg, started_at=NOW)
    candidate(db, "c1")
    fake = ScriptedModel(answer_with())
    run_author(db, home, cfg, ["c1"], fake)
    assert fake.of("author") == []
    row = db.execute("SELECT status, reason FROM candidates").fetchone()
    assert (row["status"], row["reason"]) == (
        "deferred",
        "dream dream-old merged into agentic without finishing its deploy; "
        "finish or undo that adoption first",
    )
    assert BRANCH not in branches(repo)

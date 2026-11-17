"""The Adopt step: checks on the merged result, the fast-forward merge, deploy,
rollback, resumption and revert detection (9.6–9.11, 9.13, 9.15)."""

import json

import pytest
from dreamkit import ScriptedModel, build_config
from gitkit import branches, commit, head, make_repo, out, status
from runkit import Crash, FakeClock, LocalPhase, add_run, git, make_runner, step_states
from sessiondocs import add_observation, add_seen

from dreaming import adopt, knowledge
from dreaming.steps import NewStep

RUN = "dream-now"
NOW = "2026-11-15T09:00:00+00:00"
BRANCH = adopt.branch_name(RUN)
CHECK = 'pwd -P >> "$CHECK_LOG"\nexit 0\n'
DEPLOY = (
    'git rev-parse HEAD >> "$DEPLOY_LOG"\n'
    'test ! -f fail-deploy && test -z "$DEPLOY_ALWAYS_FAILS"\n'
)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    found = {"check": tmp_path / "check.log", "deploy": tmp_path / "deploy.log"}
    monkeypatch.setenv("CHECK_LOG", str(found["check"]))
    monkeypatch.setenv("DEPLOY_LOG", str(found["deploy"]))
    monkeypatch.delenv("DEPLOY_ALWAYS_FAILS", raising=False)
    return found


def lines(path):
    return path.read_text().split() if path.exists() else []


@pytest.fixture
def repos(tmp_path):
    files = {"check.sh": CHECK, "deploy.sh": DEPLOY, "rules/python.md": "# Python\n"}
    return {name: make_repo(tmp_path, name, files) for name in ("agentic", "orbit")}


def configure(repos, *, auto_apply=True, **settings):
    entries = {
        name: {
            "path": str(path),
            "test": ["sh check.sh"],
            "deploy": ["sh deploy.sh"],
            **settings.get(name, {}),
        }
        for name, path in repos.items()
    }
    return build_config(repositories=entries, adoption={"auto_apply": auto_apply})


def change(db, home, cfg, repo, cid, files, *, depends_on=(), external=0):
    """A change committed on the run's dream branch, as the Author step makes it."""
    work = adopt.work_copy(home, cfg, RUN, repo)
    for path, text in files.items():
        (work.path / path).parent.mkdir(parents=True, exist_ok=True)
        (work.path / path).write_text(text)
    change_id = f"ch-{cid}"
    message = f"Change {cid}\n\nDreaming-Evidence: e-{cid}"
    committed = adopt.commit_change(
        home, cfg, RUN, work, change_id, message, set(files), []
    )
    # A file finding supports a change on its own (4.5).
    db.execute(
        "INSERT OR IGNORE INTO file_findings (evidence_id, repo, revision, path,"
        " location, finding, lens, lens_version) VALUES (?, ?, 'abc', 'rules/x.md',"
        " 'L1', 'a broken link', 'broken-references', 1)",
        (f"e-{cid}", repo),
    )
    db.execute(
        "INSERT INTO changes (id, run, repo, branch, commit_sha, depends_on,"
        " evidence, external_input, status, summary, commit_message, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)",
        (
            change_id,
            RUN,
            repo,
            BRANCH,
            committed.sha,
            json.dumps(list(depends_on)),
            json.dumps([f"e-{cid}"]),
            external,
            f"Change {cid}",
            message,
            NOW,
        ),
    )
    return change_id


def adopt_run(db, home, cfg, phase=None, planned=None, phases=None):
    runner = make_runner(
        db,
        home,
        RUN,
        cfg,
        phases={"adopt": phase or adopt.AdoptPhase(), **(phases or {})},
        model=ScriptedModel(lambda role, data: pytest.fail(f"{role} called")),
        clock=FakeClock(),
        planner=lambda ctx: planned or [NewStep("adopt", "adopt")],
    )
    return runner.execute()


def row(db, change_id):
    return db.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()


def adoption_steps(db, repo="agentic"):
    rows = db.execute(
        "SELECT step FROM adoption WHERE run = ? AND repo = ? ORDER BY id", (RUN, repo)
    )
    return [r[0] for r in rows]


def checked_merge(db, repo="agentic"):
    return db.execute(
        "SELECT head_after FROM adoption"
        " WHERE run = ? AND repo = ? AND step = 'checked'",
        (RUN, repo),
    ).fetchone()[0]


# Checks, merge and deploy (9.6, 9.7)


def test_checks_run_on_the_merged_result_and_the_tested_merge_is_deployed(
    db, home, repos, logs
):
    repo = repos["agentic"]
    commit(repo, {"check.sh": 'pwd -P >> "$CHECK_LOG"\nexit 1\n'}, "a failing check")
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    # The dream's change repairs the check: the merged result's own check runs.
    ch = change(db, home, cfg, "agentic", "c1", {"check.sh": CHECK})
    moved = commit(repo, {"README.md": "the user's work\n"}, "user's work")

    assert adopt_run(db, home, cfg) == "complete"
    assert step_states(db, RUN) == {"adopt": "done", "adopt:agentic": "done"}
    merge = checked_merge(db)
    # The checkout received exactly the tested merge, one merge commit on top of
    # the user's latest commit, and deployed it.
    assert head(repo) == merge
    assert out(repo, "log", "-1", "--format=%P", merge).split() == [
        moved,
        head(repo, BRANCH),
    ]
    assert lines(logs["deploy"]) == [merge]
    (checked_in,) = lines(logs["check"])
    assert checked_in == str((home / "work" / RUN / "agentic.check").resolve())
    assert status(repo) == ""
    applied = row(db, ch)
    assert (applied["status"], applied["applied_at"]) == ("applied", NOW)
    assert adoption_steps(db) == [
        "checking",
        "checked",
        "merging",
        "merged",
        "deploying",
        "deployed",
    ]
    # Work copies and the check copy go at the end; the branch stays.
    assert not (home / "work" / RUN).exists()
    assert BRANCH in branches(repo)


@pytest.mark.parametrize(
    "settings, reason",
    [
        ({"auto_apply": False}, "auto-apply is disabled"),
        (
            {"agentic": {"test": [], "lint": []}},
            "no lint or test commands are configured (9.8)",
        ),
        (
            {"agentic": {"test": ["sh check.sh", "echo failing >&2; exit 2"]}},
            "the check 'echo failing >&2; exit 2' exited 2: failing",
        ),
    ],
)
def test_unmet_conditions_keep_the_branch_unmerged_with_the_reason(
    db, home, repos, logs, settings, reason
):
    cfg = configure(repos, **settings)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    before = head(repos["agentic"])
    adopt_run(db, home, cfg)
    waiting = row(db, ch)
    assert (waiting["status"], waiting["reason"]) == ("waiting", reason)
    assert head(repos["agentic"]) == before
    assert lines(logs["deploy"]) == []
    assert BRANCH in branches(repos["agentic"])


def _dirty(repo):
    (repo / "rules" / "python.md").write_text("an uncommitted edit\n")


def _other_branch(repo):
    git(repo, "checkout", "-q", "-b", "feature")


def _in_the_way(repo):
    (repo / "rules" / "uv.md").write_text("the user's own draft\n")


@pytest.mark.parametrize(
    "prepare, reason",
    [
        (_dirty, "the checkout has uncommitted changes to tracked files"),
        (_other_branch, "the checkout is on feature, not main"),
        (_in_the_way, "untracked files in the checkout are in the way: rules/uv.md"),
    ],
)
def test_the_checkout_must_be_clean_and_on_the_default_branch(
    db, home, repos, logs, prepare, reason
):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    repo = repos["agentic"]
    prepare(repo)
    before = (head(repo), status(repo))
    adopt_run(db, home, cfg)
    assert (row(db, ch)["status"], row(db, ch)["reason"]) == ("waiting", reason)
    assert (head(repo), status(repo)) == before


def test_an_untracked_file_out_of_the_way_does_not_block(db, home, repos, logs):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    (repos["agentic"] / "scratch.txt").write_text("notes\n")
    adopt_run(db, home, cfg)
    assert row(db, ch)["status"] == "applied"
    assert (repos["agentic"] / "scratch.txt").read_text() == "notes\n"


def test_a_head_that_moved_after_the_checks_blocks_the_merge(db, home, repos, logs):
    repo = repos["agentic"]
    cfg = configure(
        repos, agentic={"test": [f"git -C {repo} commit -q --allow-empty -m moved"]}
    )
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    adopt_run(db, home, cfg)
    assert (row(db, ch)["status"], row(db, ch)["reason"]) == (
        "waiting",
        "the checkout's HEAD moved since the checks ran",
    )
    assert out(repo, "log", "-1", "--format=%s") == "moved"


def test_one_held_change_keeps_the_whole_branch_unmerged(db, home, repos, logs):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    plain = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    fetched = change(
        db, home, cfg, "agentic", "c2", {"rules/pip.md": "# pip\n"}, external=1
    )
    before = head(repos["agentic"])
    adopt_run(db, home, cfg)
    reason = f"change {fetched} received fetched web or pull-request content (9.19)"
    for change_id in (plain, fetched):
        assert (row(db, change_id)["status"], row(db, change_id)["reason"]) == (
            "waiting",
            reason,
        )
    assert head(repos["agentic"]) == before


@pytest.mark.parametrize("forget", [False, True])
def test_a_change_that_lost_its_support_keeps_the_branch_unmerged(
    db, home, repos, logs, forget
):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    evidence = []
    for sid in ("s1", "s2", "s3"):  # min_sessions distinct sessions (4.5)
        add_seen(db, "claude", sid)
        add_observation(db, f"o-{sid}", "claude", sid, kind="assistant_suggestion")
        evidence.append(f"o-{sid}")
    db.execute(
        "UPDATE changes SET evidence = ? WHERE id = ?", (json.dumps(evidence), ch)
    )
    if forget:
        # Forgotten after the change was authored, before its adoption (2.11).
        knowledge.forget_session(db, "s1")
    before = head(repos["agentic"])
    adopt_run(db, home, cfg)
    if not forget:
        assert row(db, ch)["status"] == "applied"
        return
    assert row(db, ch)["status"] == "waiting"
    assert "lost the support it was authored on" in row(db, ch)["reason"]
    assert head(repos["agentic"]) == before
    assert lines(logs["deploy"]) == []


# Deploy failure and rollback (9.9, 9.10)


def test_a_failed_deploy_is_reverted_and_the_restored_version_deployed(
    db, home, repos, logs
):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"fail-deploy": "x\n"})
    repo = repos["agentic"]
    before = head(repo)
    adopt_run(db, home, cfg)

    merge = checked_merge(db)
    revert = head(repo)
    assert out(repo, "rev-parse", f"{revert}^") == merge
    assert out(repo, "diff", before, revert) == ""
    assert f"{adopt.REVERT_TRAILER}: {RUN}" in out(repo, "log", "-1", "--format=%B")
    assert f"This reverts commit {merge}" in out(repo, "log", "-1", "--format=%B")
    # Both outcomes: the failed deploy of the merge, then the restored version.
    assert lines(logs["deploy"]) == [merge, revert]
    failed = row(db, ch)
    assert failed["status"] == "failed"
    assert failed["reason"] == (
        "the deploy command 'sh deploy.sh' exited 1; "
        "the merge was reverted and the restored version deployed"
    )
    assert adoption_steps(db)[-3:] == ["merged", "deploying", "reverted"]


def test_a_failed_recovery_stops_adoption_and_names_the_repository(
    db, home, repos, logs, monkeypatch
):
    monkeypatch.setenv("DEPLOY_ALWAYS_FAILS", "1")
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    first = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    second = change(db, home, cfg, "orbit", "c2", {"rules/uv.md": "# uv\n"})
    orbit_before = head(repos["orbit"])
    adopt_run(db, home, cfg)
    reason = row(db, first)["reason"]
    assert row(db, first)["status"] == "failed"
    assert "redeploying the restored version failed" in reason
    assert reason.endswith("agentic needs manual recovery")
    assert row(db, second)["status"] == "waiting"
    assert row(db, second)["reason"].startswith("adoption stopped: ")
    assert head(repos["orbit"]) == orbit_before


def test_linked_repositories_are_preflighted_together(db, home, repos, logs):
    cfg = configure(repos, orbit={"test": ["exit 1"]})
    add_run(db, RUN, cfg, started_at=NOW)
    first = change(
        db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"}, depends_on=["orbit"]
    )
    second = change(db, home, cfg, "orbit", "c2", {"rules/uv.md": "# uv\n"})
    heads = {name: head(path) for name, path in repos.items()}
    adopt_run(db, home, cfg)
    assert step_states(db, RUN) == {"adopt": "done", "adopt:agentic+orbit": "done"}
    assert row(db, second)["reason"] == "the check 'exit 1' exited 1"
    assert row(db, first)["reason"] == (
        "its linked repository orbit is waiting: the check 'exit 1' exited 1"
    )
    assert {name: head(path) for name, path in repos.items()} == heads


def test_linked_repositories_are_rolled_back_together(db, home, repos, logs):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    first = change(
        db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"}, depends_on=["orbit"]
    )
    second = change(db, home, cfg, "orbit", "c2", {"fail-deploy": "x\n"})
    heads = {name: head(path) for name, path in repos.items()}
    adopt_run(db, home, cfg)
    for name, path in repos.items():
        assert out(path, "diff", heads[name], "HEAD") == ""
        assert adoption_steps(db, name)[-1] == "reverted"
    assert row(db, second)["status"] == row(db, first)["status"] == "failed"
    assert row(db, first)["reason"] == (
        "reverted with orbit: the deploy command 'sh deploy.sh' exited 1; "
        "the merge was reverted and the restored version deployed"
    )


# Interruption and later dreams (9.15)


def crash_in_deploy(monkeypatch):
    deploy = adopt._deploy
    crashes = iter([True])

    def crashing(repo, cfg):
        if next(crashes, False):
            raise Crash
        return deploy(repo, cfg)

    monkeypatch.setattr(adopt, "_deploy", crashing)


def test_state_after_a_crash_is_read_from_git(db, home, repos, logs, monkeypatch):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    merge = adopt._merge

    def merge_then_crash(repo):
        merge(repo)
        raise Crash

    monkeypatch.setattr(adopt, "_merge", merge_then_crash)
    with pytest.raises(Crash):
        adopt_run(db, home, cfg)
    # Only the `merging` row was written; git shows the merge happened.
    assert adoption_steps(db)[-1] == "merging"
    (merged,) = adopt.merged_repositories(db, cfg, RUN)
    assert (merged["repo"], merged["state"], merged["merge"]) == (
        "agentic",
        "merged",
        checked_merge(db),
    )


def test_a_resume_after_a_merge_makes_no_further_merges(
    db, home, repos, logs, monkeypatch
):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    first = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    second = change(db, home, cfg, "orbit", "c2", {"rules/uv.md": "# uv\n"})
    orbit_before = head(repos["orbit"])
    crash_in_deploy(monkeypatch)
    with pytest.raises(Crash):
        adopt_run(db, home, cfg)
    assert adoption_steps(db)[-1] == "deploying"

    # A new process resumes the run.
    assert adopt_run(db, home, cfg) == "complete"
    assert head(repos["orbit"]) == orbit_before
    assert lines(logs["deploy"]) == []
    assert (row(db, first)["status"], row(db, second)["status"]) == (
        "waiting",
        "waiting",
    )
    assert (
        "not deployed; finish or undo the adoption (9.15)" in row(db, first)["reason"]
    )
    assert "interrupted after its first merge" in row(db, second)["reason"]
    (merged,) = adopt.merged_repositories(db, cfg, RUN)
    assert merged["state"] == "deploying"
    assert any("sh deploy.sh" in command for command in merged["finish"])
    assert any("revert --no-commit -m 1" in command for command in merged["undo"])


def test_a_later_dream_skips_a_repository_merged_but_not_deployed(
    db, home, repos, logs
):
    cfg = configure(repos)
    add_run(
        db, "dream-old", cfg, status="complete", started_at="2026-11-01T09:00:00+00:00"
    )
    agentic = repos["agentic"]
    git(agentic, "merge", "-q", "--no-ff", "-m", "old dream", head(agentic))
    old_merge = commit(agentic, {"old.md": "x\n"}, "old dream merge")
    db.execute(
        "INSERT INTO adoption (run, repo, branch, step, head_after, at)"
        " VALUES ('dream-old', 'agentic', 'dream/dream-old', 'merging', ?, ?)",
        (old_merge, NOW),
    )
    add_run(db, RUN, cfg, started_at=NOW)
    first = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    second = change(db, home, cfg, "orbit", "c2", {"rules/uv.md": "# uv\n"})
    adopt_run(db, home, cfg)
    assert row(db, first)["reason"] == (
        "dream dream-old merged into it without finishing its deploy; "
        "finish or undo that adoption first"
    )
    assert row(db, second)["status"] == "applied"
    assert adopt.unfinished_adoption(db, cfg, "agentic", RUN)["run"] == "dream-old"


def test_adoption_runs_after_the_budget_is_exhausted(db, home, repos, logs):
    cfg = build_config(
        repositories=configure(repos)["repositories"],
        adoption={"auto_apply": True},
        budget={"tokens": 10},
    )
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    db.execute(
        "INSERT INTO calls (run, step_key, attempt, role, backend, tokens_in,"
        " started_at, outcome)"
        " VALUES (?, 'author:x', 1, 'author', 'fake', 100, ?, 'ok')",
        (RUN, NOW),
    )
    planned = [
        NewStep("research:q", "research"),
        NewStep("adopt", "adopt", depends_on=("research:q",)),
    ]
    status = adopt_run(
        db, home, cfg, planned=planned, phases={"research": LocalPhase()}
    )
    assert status == "budget_exhausted"
    assert step_states(db, RUN)["research:q"] == "skipped"
    assert row(db, ch)["status"] == "applied"


# Reverts (9.13)


def user_reverts_the_merge(repo, merge, sha):
    git(repo, "revert", "--no-edit", "-m", "1", merge)


def user_reverts_the_change(repo, merge, sha):
    git(repo, "revert", "--no-edit", sha)


def program_reverts_the_merge(repo, merge, sha):
    """As a recovery or the 9.15 undo command does: with the trailer."""
    message = f"Revert dream\n\nThis reverts commit {merge}.\n\n"
    git(repo, "revert", "--no-commit", "-m", "1", merge)
    git(repo, "commit", "-q", "-m", f"{message}{adopt.REVERT_TRAILER}: {RUN}")


@pytest.mark.parametrize(
    "revert, expected",
    [
        (user_reverts_the_merge, "rejected"),
        (user_reverts_the_change, "rejected"),
        (program_reverts_the_merge, "failed"),
    ],
)
def test_reverts_on_the_default_branch_are_detected(
    db, home, repos, logs, revert, expected
):
    cfg = configure(repos)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    adopt_run(db, home, cfg)
    assert row(db, ch)["status"] == "applied"
    adopt.detect_reverts(db, cfg)
    assert row(db, ch)["status"] == "applied"

    revert(repos["agentic"], checked_merge(db), row(db, ch)["commit_sha"])
    adopt.detect_reverts(db, cfg)
    reverted = row(db, ch)
    assert reverted["status"] == expected
    assert reverted["reverted_at"] == out(repos["agentic"], "log", "-1", "--format=%cI")


# Merges and deletions by hand (9.13, 9.14, 10.4)

LATER = "dream-later"


def hand_merge(repo, ff):
    if ff:
        git(repo, "merge", "-q", "--ff-only", BRANCH)
        landing = out(repo, "rev-parse", "HEAD")
        commit(repo, {"README.md": "later work\n"}, "the user's later work")
    else:
        commit(repo, {"README.md": "the user's work\n"}, "the user's work")
        git(repo, "merge", "-q", "--no-ff", "-m", "merge by hand", BRANCH)
        landing = out(repo, "rev-parse", "HEAD")
    return landing


@pytest.mark.parametrize("ff", [False, True])
def test_a_branch_merged_by_hand_is_applied_and_its_revert_seen(
    db, home, repos, logs, ff
):
    repo = repos["agentic"]
    cfg = configure(repos, auto_apply=False)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    adopt_run(db, home, cfg)
    assert row(db, ch)["status"] == "waiting"
    adopt.detect_merges(db, cfg, LATER, NOW)
    assert row(db, ch)["status"] == "waiting"

    landing = hand_merge(repo, ff)
    adopt.detect_merges(db, cfg, LATER, NOW)
    merged = row(db, ch)
    assert merged["status"] == "applied"
    assert adopt.landed(repo, merged["commit_sha"], "main")[0] == landing
    assert merged["applied_at"] == out(repo, "log", "-1", "--format=%cI", landing)
    # The run that made the change leaves it to its own Adopt step (9.15).
    db.execute("UPDATE changes SET status = 'waiting' WHERE id = ?", (ch,))
    adopt.detect_merges(db, cfg, RUN, NOW)
    assert row(db, ch)["status"] == "waiting"
    adopt.detect_merges(db, cfg, LATER, NOW)

    if ff:
        git(repo, "revert", "--no-edit", merged["commit_sha"])
    else:
        git(repo, "revert", "--no-edit", "-m", "1", landing)
    adopt.detect_reverts(db, cfg)
    assert row(db, ch)["status"] == "rejected"


def test_a_branch_deleted_by_hand_is_a_rejection(db, home, repos, logs):
    cfg = configure(repos, auto_apply=False)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    adopt_run(db, home, cfg)
    git(repos["agentic"], "branch", "-D", BRANCH)
    later = "2026-11-20T09:00:00+00:00"
    adopt.detect_merges(db, cfg, LATER, later)
    assert (row(db, ch)["status"], row(db, ch)["reverted_at"]) == ("rejected", later)


def test_a_merge_the_program_made_keeps_its_own_record(db, home, repos, logs):
    """Merged before an interruption and not deployed: 9.15, not a hand merge."""
    repo = repos["agentic"]
    cfg = configure(repos, auto_apply=False)
    add_run(db, RUN, cfg, started_at=NOW)
    ch = change(db, home, cfg, "agentic", "c1", {"rules/uv.md": "# uv\n"})
    db.execute("UPDATE changes SET status = 'waiting' WHERE id = ?", (ch,))
    git(repo, "merge", "-q", "--no-ff", "-m", "dream merge", BRANCH)
    db.execute(
        "INSERT INTO adoption (run, repo, branch, step, head_after, at)"
        " VALUES (?, 'agentic', ?, 'merging', ?, ?)",
        (RUN, BRANCH, head(repo), NOW),
    )
    adopt.detect_merges(db, cfg, LATER, NOW)
    assert row(db, ch)["status"] == "waiting"

import json
import subprocess
from dataclasses import replace

import pytest

from dreaming.config import load_config
from dreaming.store import Store


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


@pytest.fixture
def adoption_env(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "rule.md").write_text("# Rule\n\nUse short examples.\nKeep the ending.\n")
    (repo / "Makefile").write_text(
        "generate:\n\t@echo generated > derived\nlint:\n\t@test -f derived\n"
        "test:\n\t@true\ndeploy:\n\t@true\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        repositories={
            "target": {
                "path": str(repo),
                "default_branch": "main",
                "generate": ["make generate"],
                "lint": ["make lint"],
                "test": ["make test"],
                "deploy": ["make deploy"],
            }
        },
    )
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        add_change(store, repo)
        yield store, config, repo


def add_change(store, repo, cid="c", after="Use concrete examples.", **fields):
    edits = [
        {
            "id": "edit",
            "path": "rule.md",
            "before": "Use short examples.",
            "after": after,
            "supporting_evidence": ["e1"],
            "counter_evidence": ["e2"],
        }
    ]
    store.connection.execute(
        "INSERT INTO changes(id,run_id,repo,edits,evidence,authoring_id) VALUES (?,'r',?,?,?,?)",
        (cid, str(repo), json.dumps(edits), '["e1","e2"]', cid),
    )
    for key, value in fields.items():
        store.connection.execute(f"UPDATE changes SET {key}=? WHERE id=?", (value, cid))


def test_branch_commits_generate_and_leave_user_checkout_untouched(adoption_env):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    head = git(repo, "rev-parse", "HEAD")
    adoption = Adoption(store, "r", config)
    adoption.author_branches()
    branch, commit = store.connection.execute("SELECT branch,commit_sha FROM changes").fetchone()
    assert branch == "dream/r" and commit
    assert git(repo, "rev-parse", "HEAD") == head
    assert "concrete" in git(repo, "show", f"{branch}:rule.md")
    assert git(repo, "show", f"{branch}:derived") == "generated"
    assert "e1" in git(repo, "log", "-1", "--format=%B", branch)
    adoption.author_branches()
    assert git(repo, "rev-parse", branch) == commit
    adoption.cleanup()
    assert branch in git(repo, "branch")
    assert len(git(repo, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "A private NameHere",
        "token=secret-value",
        "Long transcript quote that must never be published",
    ],
)
def test_public_scan_withholds_and_saves_private_patch(adoption_env, tmp_path, text):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    names = tmp_path / "names.txt"
    names.write_text("NameHere\n")
    config = replace(
        config,
        names_file=names,
        scan={"quote_chars": 15},
        repositories={"target": {**config.repositories["target"], "public": True}},
    )
    store.connection.execute(
        "UPDATE changes SET edits=?",
        (
            json.dumps(
                [{"id": "x", "path": "rule.md", "before": "Use short examples.", "after": text}]
            ),
        ),
    )
    adoption = Adoption(store, "r", config, transcript_texts=[text])
    adoption.author_branches()
    assert store.connection.execute("SELECT status,commit_sha FROM changes").fetchone() == (
        "withheld",
        None,
    )
    assert list((store.path.parent / "patches").rglob("*.patch"))
    assert git(repo, "rev-list", "--count", "dream/r") == "1"
    adoption.cleanup()


def test_checked_merge_is_the_deployed_commit(adoption_env):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    before = git(repo, "rev-parse", "HEAD")
    adoption = Adoption(store, "r", config)
    adoption.run()
    assert store.connection.execute("SELECT status FROM changes").fetchone()[0] == "applied"
    merged = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", "HEAD^1") == before
    assert len(git(repo, "rev-list", "--parents", "-1", merged).split()) == 3
    checked = store.connection.execute(
        "SELECT head_after FROM adoption WHERE step='checked'"
    ).fetchone()[0]
    assert checked == merged
    assert (
        store.connection.execute("SELECT step FROM adoption ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "deployed"
    )


@pytest.mark.parametrize(
    "condition", ["dirty", "branch", "no_checks", "failing_check", "external", "held", "disabled"]
)
def test_unmet_conditions_keep_whole_branch_unmerged(adoption_env, condition):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    head = git(repo, "rev-parse", "HEAD")
    if condition == "dirty":
        (repo / "rule.md").write_text("User work")
    if condition == "branch":
        git(repo, "checkout", "-b", "user-work")
    if condition in {"no_checks", "failing_check"}:
        settings = {**config.repositories["target"], "lint": [], "test": []}
        if condition == "failing_check":
            settings.update(lint=["false"], test=["true"])
        config = replace(config, repositories={"target": settings})
    if condition == "external":
        store.connection.execute("UPDATE changes SET external_input=1")
    if condition == "held":
        store.connection.execute("UPDATE changes SET status='withheld',reason='replay_failed'")
    if condition == "disabled":
        config = replace(config, auto_apply=False)
    Adoption(store, "r", config).run()
    assert git(repo, "rev-parse", "HEAD") == head
    assert store.connection.execute("SELECT status FROM changes").fetchone()[0] != "applied"
    assert store.connection.execute("SELECT reason FROM changes").fetchone()[0]


def test_failed_deploy_reverts_merge_and_redeploys_restored_version(adoption_env):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    config = replace(
        config,
        repositories={
            "target": {**config.repositories["target"], "deploy": ["! grep -q 'concrete' rule.md"]}
        },
    )
    Adoption(store, "r", config).run()
    assert "short" in (repo / "rule.md").read_text()
    assert "Dreaming-Revert: r" in git(repo, "log", "-1", "--format=%B")
    assert store.connection.execute("SELECT status FROM changes").fetchone()[0] == "failed"
    assert (
        "restored_deploy=success"
        in store.connection.execute(
            "SELECT outcome FROM adoption WHERE step='reverted'"
        ).fetchone()[0]
    )


def test_resume_after_merge_provides_commands_without_deploying(adoption_env):
    from dreaming.adopt import Adoption, discard

    store, config, repo = adoption_env
    Adoption(store, "r", config).run()
    store.connection.execute("UPDATE runs SET statistics=?", ('{"resumed":true}',))
    head = git(repo, "rev-parse", "HEAD")
    commands = Adoption(store, "r", config).run()
    assert git(repo, "rev-parse", "HEAD") == head
    assert "git -C" in json.dumps(commands) and "revert" in json.dumps(commands)
    with pytest.raises(ValueError, match="merged"):
        discard(store, dict(id="r", snapshot_path=None), config=config)


def test_moved_head_after_checks_is_named_and_never_merged(adoption_env, monkeypatch):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    adoption = Adoption(store, "r", config)
    check = adoption._check

    def move_head(path):
        checked = check(path)
        git(repo, "commit", "--allow-empty", "-m", "concurrent user commit")
        return checked

    monkeypatch.setattr(adoption, "_check", move_head)
    adoption.run()
    assert git(repo, "log", "-1", "--format=%s") == "concurrent user commit"
    assert "HEAD_moved" in store.connection.execute("SELECT reason FROM changes").fetchone()[0]
    assert not store.connection.execute("SELECT 1 FROM adoption WHERE step='merging'").fetchone()


@pytest.mark.parametrize("failure", ["preflight", "deploy"])
def test_linked_repositories_preflight_and_rollback_together(adoption_env, tmp_path, failure):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    second = tmp_path / "second"
    git(tmp_path, "clone", str(repo), str(second))
    git(second, "config", "user.email", "test@example.invalid")
    git(second, "config", "user.name", "Test")
    settings = {**config.repositories["target"], "path": str(second)}
    if failure == "deploy":
        settings["deploy"] = ["! grep -q 'concrete' rule.md"]
    else:
        (second / "rule.md").write_text("User work")
    config = replace(config, repositories={**config.repositories, "second": settings})
    add_change(store, second, cid="c2", depends_on='["c"]')
    before = git(repo, "rev-parse", "HEAD")
    Adoption(store, "r", config).run()
    if failure == "preflight":
        assert git(repo, "rev-parse", "HEAD") == before
        assert not store.connection.execute("SELECT 1 FROM adoption WHERE step='merged'").fetchone()
    else:
        assert "short" in (repo / "rule.md").read_text()
        assert "short" in (second / "rule.md").read_text()
        assert (
            store.connection.execute(
                "SELECT count(*) FROM adoption WHERE step='reverted'"
            ).fetchone()[0]
            == 2
        )


def test_default_branch_discovery_and_public_binary_baseline(adoption_env):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    (repo / "asset.bin").write_bytes(b"\0\xff\xfe")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "binary baseline")
    settings = {**config.repositories["target"], "public": True}
    settings.pop("default_branch")
    config = replace(config, repositories={"target": settings})
    Adoption(store, "r", config).run()
    assert store.connection.execute("SELECT status FROM changes").fetchone()[0] == "applied"


def test_later_run_does_not_branch_in_repository_with_pending_deployment(adoption_env):
    from dreaming.adopt import Adoption

    store, config, repo = adoption_env
    store.connection.execute("INSERT INTO runs(id,status) VALUES ('old','partial')")
    store.connection.execute(
        "INSERT INTO adoption(run_id,repo,branch,step,at) "
        "VALUES ('old',?,'dream/old','merged','now')",
        (str(repo),),
    )
    Adoption(store, "r", config).run()
    assert "dream/r" not in git(repo, "branch")
    assert (
        store.connection.execute("SELECT reason FROM changes").fetchone()[0]
        == "earlier_merge_not_deployed"
    )


def test_scan_checks_messages_and_allows_existing_transcript_text():
    from dreaming.scan import scan_output

    text = "A long example that was already committed to the repository"
    assert scan_output(text, transcripts=[text], quote_chars=10) == ["transcript_text"]
    assert scan_output(text, transcripts=[text], existing=text, quote_chars=10) == []
    assert scan_output("Evidence: token=abc") == ["credentials"]


def test_discard_removes_unmerged_branch_and_worktree(adoption_env):
    from dreaming.adopt import Adoption, discard

    store, config, repo = adoption_env
    Adoption(store, "r", config).author_branches()
    assert len(git(repo, "worktree", "list").splitlines()) == 2
    discard(store, {"id": "r"}, config=config)
    assert "dream/r" not in git(repo, "branch")
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_reconcile_merge_intent_reads_git_without_deploying(adoption_env):
    from dreaming.adopt import Adoption, reconcile_merges

    store, config, repo = adoption_env
    adoption = Adoption(store, "r", config)
    adoption.author_branches()
    before, tested = adoption._check(repo)
    adoption._event(repo, "merging", before=before, after=tested)
    reconcile_merges(store, "r")
    assert not store.connection.execute("SELECT 1 FROM adoption WHERE step='merged'").fetchone()
    git(repo, "merge", "--ff-only", tested)
    reconcile_merges(store, "r")
    assert (
        store.connection.execute("SELECT outcome FROM adoption WHERE step='merged'").fetchone()[0]
        == "recovered_from_git"
    )
    assert store.connection.execute("SELECT status FROM changes").fetchone()[0] == "applied"
    reconcile_merges(store, "r")
    assert (
        store.connection.execute("SELECT count(*) FROM adoption WHERE step='merged'").fetchone()[0]
        == 1
    )
    adoption.cleanup()


@pytest.mark.parametrize("trailer,status", [(False, "rejected"), (True, "failed")])
def test_reverts_detected_on_default_branch(adoption_env, trailer, status):
    from dreaming.adopt import Adoption, detect_reverts

    store, config, repo = adoption_env
    Adoption(store, "r", config).run()
    git(repo, "revert", "--no-edit", "-m", "1", "HEAD")
    if trailer:
        message = git(repo, "log", "-1", "--format=%B") + "\n\nDreaming-Revert: r"
        git(repo, "commit", "--amend", "-m", message)
    detect_reverts(store, config)
    assert (
        store.connection.execute("SELECT status,reverted_at FROM changes").fetchone()[0] == status
    )

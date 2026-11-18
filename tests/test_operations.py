"""Run and session operations through `dream api` and the CLI (1.1, 1.6, 2.10,
2.11, 5.3, 5.4, 5.5, 9.15, 14.1, 14.8)."""

import io
import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest
from gitkit import branches, commit, head, make_repo, out, status
from runkit import (
    CallPhase,
    FakeClock,
    FakeModel,
    add_run,
    dump,
    git,
    instruction_repo,
    reads,
    run_row,
)
from sessiondocs import add_item, add_observation, add_seen

from dreaming import adopt, api, cli, config, run, store
from dreaming.backends import BackendError
from dreaming.backends.probe import ProbeResult
from dreaming.budget import tokens_used

RUN = "dream-20261115-090000-ab12"
SLEEPER = "import time\ntime.sleep(60)\n"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path, "agentic", {"rules/python.md": "# Python\n"})


@pytest.fixture
def cfg(tmp_path, home, repo):
    """A configuration with an instruction repository and one target repository,
    also written to the home so `dream` commands read it."""
    instructions = instruction_repo(tmp_path)
    deployed = tmp_path / "deployed.txt"
    (home / "config.toml").write_text(
        textwrap.dedent(f"""
            [instructions]
            repo = "{instructions}"

            [repositories.agentic]
            path = "{repo}"
            generate = ["printf generated > generated.txt"]
            lint = ["true"]
            test = ["true"]
            deploy = ["printf deployed > {deployed}"]
        """)
    )
    return config.load_config(home)


@pytest.fixture
def sleeper(tmp_path, monkeypatch):
    """Detached children that sleep; killed afterwards."""
    script = tmp_path / "sleeper.py"
    script.write_text(SLEEPER)
    monkeypatch.setattr(run, "CHILD", (sys.executable, str(script)))
    yield script
    conn = store.open_store(store.store_path(config.dreaming_home()))
    for row in conn.execute("SELECT process_group FROM runs"):
        if row[0]:
            try:
                os.kill(row[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
    conn.close()


def call(conn, cfg, op, **fields):
    return api.handle(conn, cfg, {"dreaming_api": 1, "op": op, **fields})


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def snapshot_run(home, cfg, run_id=RUN, **fields):
    """A run row with its snapshot, as `run.start` leaves them."""
    snapshot = config.write_snapshot(home, run_id, cfg)
    conn = store.open_store(store.store_path(home))
    add_run(conn, run_id, cfg, snapshot_path=str(snapshot), **fields)
    conn.close()


# Starting (1.1, 1.6)


def test_dream_start_returns_a_run_id_and_the_dream_outlives_its_client(
    home, cfg, tmp_path, sleeper
):
    client = tmp_path / "client.py"
    client.write_text(
        textwrap.dedent(f"""
            import sys
            from dreaming import api, run
            run.CHILD = (sys.executable, {str(sleeper)!r})
            sys.exit(api.main(sys.stdin, sys.stdout))
        """)
    )
    request = json.dumps({"dreaming_api": 1, "op": "dream.start"})
    done = subprocess.run(
        [sys.executable, str(client)],
        input=request,
        capture_output=True,
        text=True,
        timeout=60,
    )
    response = json.loads(done.stdout)
    assert (done.returncode, response["ok"], response["resumed"]) == (0, True, False)
    conn = store.open_store(store.store_path(home))
    row = run_row(conn, response["run_id"])
    conn.close()
    # The client has exited; the dream runs on in a session of its own and
    # holds the lock the client took for it.
    pid = row["process_group"]
    assert alive(pid)
    assert os.getsid(pid) == pid
    assert run.acquire_lock(home) is None
    assert (row["status"], row["requester"]) == ("running", "user")


def test_the_requester_is_recorded_and_a_second_start_names_the_running_dream(
    db, home, cfg, sleeper
):
    started = call(db, cfg, "dream.start", requester="screw")
    assert started["ok"]
    assert run_row(db, started["run_id"])["requester"] == "screw"
    before = dump(db)
    second = call(db, cfg, "dream.start")
    assert (second["ok"], second["code"]) == (False, "already_running")
    assert started["run_id"] in second["error"]
    assert dump(db) == before


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"selection": {"since": "last week"}}, "invalid_request"),
        ({"selection": {"colour": "blue"}}, "invalid_request"),
        ({"selection": {"scope": {"team": "x"}}}, "invalid_scope"),
        ({"overrides": {"tools": {}}}, "invalid_override"),
        ({"requester": 7}, "invalid_request"),
    ],
)
def test_a_start_outside_the_contract_starts_nothing(db, home, cfg, fields, code):
    response = call(db, cfg, "dream.start", **fields)
    assert (response["ok"], response["code"]) == (False, code)
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# Inspecting (5.3, 14.1)


def test_status_shows_a_waiting_dream_with_its_reset_time_and_the_fallback_option(
    db, home, cfg, capsys
):
    run_id = add_run(db, RUN, cfg)
    clock = FakeClock()  # 09:00 UTC
    limited = FakeModel(clock)
    limited.name = "claude"
    fallback = FakeModel(clock)
    fallback.name = "codex"
    limits = iter([BackendError("You've hit your limit · resets 1pm (UTC)")])

    def before(role, prompt):
        error = next(limits, None)
        if error:
            raise error

    limited.before = before
    seen = []

    def observe(seconds):
        if not seen:
            seen.append(call(db, cfg, "dream.status"))
            cli.main(["status"])

    clock.on_sleep = observe
    roles = {role: {"backends": ["claude", "codex"]} for role in config.ROLES}
    runner = run.Runner(
        db,
        home,
        run_id,
        {**cfg, "roles": roles},
        phases={"read": CallPhase("reader")},
        backends={"claude": limited, "codex": fallback},
        clock=clock,
        planner=reads(1, review=False),
    )
    assert runner.execute() == "complete"
    state = seen[0]
    assert state["status"] == "waiting"
    assert state["waiting"]["until"].startswith("2026-11-15T13:01")
    assert state["waiting"]["option"] == "fallback_on_usage_limit"
    assert "continue on codex" in state["waiting"]["message"]
    shown = capsys.readouterr().out
    assert "waiting until 2026-11-15T13:01" in shown
    assert "fallback_on_usage_limit" in shown
    assert call(db, cfg, "dream.status")["waiting"] is None


def test_status_reports_progress_and_unknown_runs_are_not_found(db, home, cfg):
    add_run(db, RUN, cfg, requester="screw")
    state = call(db, cfg, "dream.status", run_id=RUN)
    assert (state["run_id"], state["status"], state["requester"]) == (
        RUN,
        "running",
        "screw",
    )
    assert state["tokens_used"] == 0
    assert state["budget"]["tokens"] == cfg["budget"]["tokens"]
    missing = call(db, cfg, "dream.status", run_id="dream-nope")
    assert (missing["ok"], missing["code"]) == (False, "not_found")
    assert "dream-nope" in missing["error"]


def test_report_gives_the_written_report_or_not_found(db, home, cfg, tmp_path):
    add_run(db, RUN, cfg)
    assert call(db, cfg, "dream.report")["code"] == "not_found"
    report = tmp_path / "report.html"
    report.write_text("<html></html>")
    db.execute("UPDATE runs SET report_path = ? WHERE id = ?", (str(report), RUN))
    found = call(db, cfg, "dream.report", run_id=RUN)
    assert (found["ok"], found["report"]) == (True, str(report))
    assert call(db, cfg, "dream.report", run_id="dream-nope")["code"] == "not_found"


# Resuming (1.6, 5.4, 5.5)


def test_resume_takes_only_backend_choices(db, home, cfg, sleeper):
    snapshot_run(home, cfg, status="interrupted", overrides={"budget": {"tokens": 9}})
    before = dump(db)
    refused = call(db, cfg, "dream.resume", overrides={"budget": {"tokens": 5}})
    assert (refused["ok"], refused["code"]) == (False, "invalid_override")
    assert dump(db) == before
    resumed = call(db, cfg, "dream.resume", overrides={"roles": {"reader": ["codex"]}})
    assert (resumed["ok"], resumed["run_id"], resumed["resumed"]) == (True, RUN, True)
    assert resumed["overrides"] == {
        "budget": {"tokens": 9},
        "roles": {"reader": ["codex"]},
    }
    assert run_row(db, RUN)["status"] == "running"


def test_only_an_interrupted_dream_is_resumed(db, home, cfg):
    assert call(db, cfg, "dream.resume")["code"] == "not_found"
    snapshot_run(home, cfg, run_id="dream-done", status="complete")
    named = call(db, cfg, "dream.resume", run_id="dream-done")
    assert (named["code"], "complete" in named["error"]) == ("run_finished", True)
    assert call(db, cfg, "dream.resume", run_id="dream-nope")["code"] == "not_found"


def _probe_result(name, usable, reason=""):
    return ProbeResult(
        name,
        "claude",
        "2.1.311",
        status="probed" if usable else "unavailable",
        isolation="safe-mode",
        agent_enforcement="removed",
        apsis_finds_session=True,
        tokens_in=40,
        tokens_out=10,
        reason=reason,
    )


def test_each_backend_is_checked_on_first_use_and_again_in_a_resumed_process(
    db, home, capsys
):
    cfg = config.build_config(
        {
            "backends": {
                name: {"harness": "claude", "context_tokens": 100_000}
                for name in ("flaky", "steady")
            },
            "roles": {role: {"backends": ["flaky", "steady"]} for role in config.ROLES},
        }
    )
    run_id = add_run(db, RUN, cfg)
    probed = []
    usable = {"flaky": False, "steady": True}

    def probe_backend(home, cfg, name):
        probed.append(name)
        return _probe_result(name, usable[name], "" if usable[name] else "logged out")

    def adapter(home, cfg, run_id, result):
        if not result.usable:
            return None
        model = FakeModel()
        model.name = result.backend
        return model

    def backends():
        return run.ProbedBackends(
            db, home, cfg, run_id, probe_backend=probe_backend, adapter=adapter
        )

    first = backends()
    runner = run.Runner(db, home, run_id, cfg, phases={}, backends=first)
    ctx = run.Context(runner, None)
    assert [b.name for b in ctx.backends("reader")] == ["steady"]
    assert [b.name for b in ctx.backends("reviewer")] == ["steady"]
    assert probed == ["flaky", "steady"]
    assert "backend flaky is not usable: logged out" in capsys.readouterr().out
    rows = db.execute(
        "SELECT step_key, role, outcome, error FROM calls ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("probe:flaky", "probe", "unavailable", "logged out"),
        ("probe:steady", "probe", "ok", None),
    ]
    # The probes' tokens count against the budget (5.5).
    assert tokens_used(db, run_id) == 100

    # A resumed process checks every backend again, including the one the
    # earlier part found unusable, which may have come back (5.4).
    usable["flaky"] = True
    resumed = backends()
    assert "flaky" in resumed and "steady" in resumed
    assert probed == ["flaky", "steady", "flaky", "steady"]


# Discarding (5.4, 9.15)


def _dream_branch(home, repo, run_id=RUN):
    """The run's work copy and dream branch with one commit on it."""
    work = adopt.work_root(home, run_id) / "agentic"
    git(repo, "worktree", "add", "-q", "-b", adopt.branch_name(run_id), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    return work, commit(work, {"rules/python.md": "# Python\n\n- Use uv.\n"})


def _rows_of_the_run(db, run_id=RUN):
    db.execute(
        "INSERT INTO steps (run, key, phase, state)"
        " VALUES (?, 'read:1', 'read', 'done')",
        (run_id,),
    )
    db.execute(
        "INSERT INTO calls (run, step_key, attempt, role, backend, started_at)"
        " VALUES (?, 'read:1', 1, 'reader', 'claude', '2026-11-15')",
        (run_id,),
    )
    # A pattern the run created, holding its own and an earlier observation.
    db.execute(
        "INSERT INTO patterns (id, key, lens, created_at, created_run)"
        " VALUES ('p-new', 'prefers uv', 'prefs', '2026-11-15', ?)",
        (run_id,),
    )
    add_seen(db, "claude", "s-old")
    add_seen(db, "claude", "s-new", entries_read=7)
    db.execute(
        "UPDATE sessions_seen SET last_run = ?, lens_versions = '{\"prefs\": 1}',"
        " size_bytes = 900, forgotten = 1 WHERE session_id = 's-new'",
        (run_id,),
    )
    add_observation(db, "o-old", "claude", "s-old", pattern_id="p-new")
    add_observation(db, "o-new", "claude", "s-new", pattern_id="p-new")
    db.execute("UPDATE observations SET run = ? WHERE evidence_id = 'o-new'", (run_id,))
    # An item the run created, and an earlier item the run revised.
    add_item(db, "k-old", ["o-old"])
    add_item(db, "k-new", ["o-new"])
    db.execute(
        "UPDATE knowledge_revisions SET originating_run = ? WHERE item_id = 'k-new'",
        (run_id,),
    )
    db.execute(
        "INSERT INTO knowledge_revisions (item_id, version, content, kind, status,"
        " confidence, supporting, actor, originating_run, base_version, created_at)"
        " VALUES ('k-old', 2, 'revised', 'preference', 'contested', 'low', '[]',"
        " 'dream', ?, 1, '2026-11-15')",
        (run_id,),
    )
    db.execute(
        "UPDATE knowledge_items SET version = 2, status = 'contested',"
        " summary = 'revised' WHERE id = 'k-old'"
    )
    db.execute(
        "INSERT INTO changes (id, run, repo, status, created_at)"
        " VALUES ('ch-1', ?, 'agentic', 'waiting', '2026-11-15')",
        (run_id,),
    )


def _count(db, table, run_id=RUN):
    query = f"SELECT COUNT(*) FROM {table} WHERE run = ?"
    return db.execute(query, (run_id,)).fetchone()[0]


def test_discard_removes_the_runs_branches_work_copies_and_rows(
    db, home, cfg, repo, capsys
):
    snapshot_run(home, cfg, status="interrupted")
    user_head = head(repo)
    work, tip = _dream_branch(home, repo)
    _rows_of_the_run(db)
    # Checks ran, but nothing was merged.
    db.execute(
        "INSERT INTO adoption (run, repo, branch, step, at)"
        " VALUES (?, 'agentic', ?, 'checked', '2026-11-15')",
        (RUN, adopt.branch_name(RUN)),
    )

    assert cli.main(["discard"]) == 0
    assert f"deleted {adopt.branch_name(RUN)} in agentic" in capsys.readouterr().out

    assert branches(repo) == ["main"]
    assert not work.exists()
    assert out(repo, "worktree", "list").count("\n") == 0
    assert (head(repo), status(repo)) == (user_head, "")
    row = run_row(db, RUN)
    assert (row["status"], row["ended_at"] is not None) == ("discarded", True)
    for table in ("steps", "calls", "observations", "changes", "adoption"):
        assert _count(db, table) == 0, table
    # The item the run created is gone; the one it revised is back at its
    # earlier revision and queued for reassessment.
    items = {
        r["id"]: tuple(r)[1:]
        for r in db.execute(
            "SELECT id, version, status, summary, pending_reassessment"
            " FROM knowledge_items"
        )
    }
    assert items == {"k-old": (1, "active", "content k-old", 1)}
    assert db.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT pattern_id FROM observations WHERE evidence_id = 'o-old'"
        ).fetchone()[0]
        is None
    )
    # The session the run read is unread again, and stays forgotten.
    seen = db.execute(
        "SELECT entries_read, lens_versions, size_bytes, last_run, forgotten"
        " FROM sessions_seen WHERE session_id = 's-new'"
    ).fetchone()
    assert tuple(seen) == (0, "{}", None, None, 1)


def test_discard_after_a_merge_is_refused_with_the_commands_to_finish_or_undo(
    db, home, cfg, repo, tmp_path
):
    snapshot_run(home, cfg, status="interrupted")
    before_merge = head(repo)
    _dream_branch(home, repo)
    git(repo, "merge", "-q", "--no-ff", "-m", "dream", adopt.branch_name(RUN))
    merge = head(repo)
    # The crash came after git merged and before the `merged` row: git says so.
    db.execute(
        "INSERT INTO adoption (run, repo, branch, step, head_before, head_after, at)"
        " VALUES (?, 'agentic', ?, 'merging', ?, ?, '2026-11-15')",
        (RUN, adopt.branch_name(RUN), before_merge, merge),
    )
    before = dump(db)

    refused = call(db, cfg, "dream.discard")
    assert (refused["ok"], refused["code"]) == (False, "already_merged")
    assert dump(db) == before
    assert adopt.branch_name(RUN) in branches(repo)
    [merged] = refused["repositories"]
    assert (merged["repo"], merged["state"], merged["merge"]) == (
        "agentic",
        "merged",
        merge,
    )
    assert merged["finish"] == [
        f"cd {repo} && printf deployed > {tmp_path / 'deployed.txt'}"
    ]
    assert "revert --no-commit -m 1" in merged["undo"][0]
    assert "undo" in refused["error"] and merge in refused["error"]

    # The undo commands revert the merge with the trailer that marks a failed
    # adoption (9.13), regenerate before committing, then deploy.
    for command in merged["undo"]:
        subprocess.run(command, shell=True, check=True, capture_output=True)
    assert (repo / "rules" / "python.md").read_text() == "# Python\n"
    assert (repo / "generated.txt").read_text() == "generated"
    message = out(repo, "log", "-1", "--format=%B")
    assert f"{adopt.REVERT_TRAILER}: {RUN}" in message
    assert (tmp_path / "deployed.txt").read_text() == "deployed"


def test_only_an_interrupted_dream_can_be_discarded(db, home, cfg):
    assert call(db, cfg, "dream.discard")["code"] == "not_found"
    snapshot_run(home, cfg, run_id="dream-done", status="complete")
    finished = call(db, cfg, "dream.discard", run_id="dream-done")
    assert (finished["code"], run_row(db, "dream-done")["status"]) == (
        "run_finished",
        "complete",
    )
    snapshot_run(home, cfg, status="interrupted")
    fd = run.acquire_lock(home)
    try:
        running = call(db, cfg, "dream.discard")
    finally:
        os.close(fd)
    assert running["code"] == "already_running"
    assert run_row(db, RUN)["status"] == "interrupted"


# Sessions (2.11, 14.8)


def test_registering_forgetting_and_restoring_are_idempotent(db, cfg):
    add_seen(db, "claude", "s1")
    request = {
        "tool": "orbit",
        "run_ref": "run-7",
        "sessions": [{"source": "claude", "session_id": "s1", "workflow": "spec"}],
    }
    first = call(db, cfg, "session.register", **request)
    assert first["sessions"][0]["state"] == "registered"
    changes = db.total_changes
    again = call(db, cfg, "session.register", **request)
    assert again["sessions"][0]["state"] == "unchanged"
    assert db.total_changes == changes

    assert call(db, cfg, "session.forget", session_id="s1")["changed"] is True
    changes = db.total_changes
    assert call(db, cfg, "session.forget", session_id="s1")["changed"] is False
    assert db.total_changes == changes
    assert call(db, cfg, "session.restore", session_id="s1")["changed"] is True
    assert call(db, cfg, "session.restore", session_id="s1")["changed"] is False


@pytest.mark.parametrize(
    ("op", "fields", "code"),
    [
        ("session.forget", {"session_id": "nope"}, "not_found"),
        ("session.restore", {"session_id": "nope"}, "not_found"),
        ("session.forget", {}, "invalid_request"),
        ("session.register", {"tool": "orbit", "run_ref": "r", "sessions": []}, None),
        ("session.register", {"tool": "orbit", "sessions": [{"file": "x"}]}, None),
    ],
)
def test_session_requests_outside_the_contract_get_stable_codes(
    db, cfg, op, fields, code
):
    response = call(db, cfg, op, **fields)
    assert (response["ok"], response["code"]) == (False, code or "invalid_request")


def test_the_cli_forgets_and_restores_a_session(db, home, cfg, capsys):
    add_seen(db, "claude", "s1")
    assert cli.main(["forget", "s1"]) == 0
    assert "forgot s1" in capsys.readouterr().out
    assert cli.main(["forget", "s1"]) == 0
    assert "nothing changed" in capsys.readouterr().out
    assert cli.main(["restore", "s1", "--source", "claude"]) == 0
    assert cli.main(["forget", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_the_cli_resumes_only_an_interrupted_dream(db, home, cfg, sleeper, capsys):
    assert cli.main(["resume"]) == 2
    assert "no interrupted dream" in capsys.readouterr().err
    snapshot_run(home, cfg, status="interrupted")
    assert cli.main(["resume", "--backend", "reader=codex"]) == 0
    assert f"resumed {RUN}" in capsys.readouterr().out


def test_dream_api_writes_the_error_details_and_exits_with_its_code(
    home, cfg, monkeypatch, capsys
):
    request = {"dreaming_api": 1, "op": "dream.discard"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert cli.main(["api"]) == 2
    response = json.loads(capsys.readouterr().out)
    assert (response["ok"], response["code"]) == (False, "not_found")

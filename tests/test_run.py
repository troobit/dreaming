"""Starting, locking, resuming and exit codes (1.1, 1.6, 5.3, 5.4, 7.3, 7.5)."""

import json
import os
import signal
import sys
import textwrap
import time

import pytest
from runkit import (
    CallPhase,
    FakeClock,
    FakeModel,
    add_run,
    dump,
    fake_config,
    files,
    instruction_repo,
    make_runner,
    reads,
    run_row,
)

from dreaming import cli, config, run, steps, store, versions
from dreaming.errors import PARTIAL, SKIPPED, USAGE_ERROR, DreamingError, Partial


@pytest.fixture
def cfg(tmp_path):
    repo = instruction_repo(tmp_path)
    return config.build_config({"instructions": {"repo": str(repo)}})


@pytest.fixture
def children(tmp_path):
    """Writes fake child programs; started children are killed afterwards."""
    started = []

    def make(body):
        script = tmp_path / f"child{len(started)}.py"
        script.write_text(textwrap.dedent(body))
        started.append(script)
        return [sys.executable, str(script)]

    pids = []
    make.pids = pids
    yield make
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


SLEEPER = """
    import time
    time.sleep(60)
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _read_pid(path):
    assert _wait_for(lambda: path.exists() and path.read_text().strip())
    return int(path.read_text())


def _stop(started):
    started.process.kill()
    started.process.wait()


def _add(home, cfg, run_id, **fields):
    """A run row with its snapshot, as `run.start` leaves them."""
    snapshot = config.write_snapshot(home, run_id, cfg)
    conn = store.open_store(home / "dreaming.db")
    add_run(conn, run_id, cfg, snapshot_path=str(snapshot), **fields)
    conn.close()


# The lock


def test_the_lock_is_taken_without_waiting(home):
    first = run.acquire_lock(home)
    assert first is not None
    assert run.acquire_lock(home) is None
    os.close(first)
    second = run.acquire_lock(home)
    assert second is not None
    os.close(second)
    # The lock file exists only to be locked: it stays empty.
    assert (home / "lock").read_bytes() == b""


def test_the_detached_child_holds_the_lock_after_the_parent_and_loses_it_when_killed(
    home, cfg, children
):
    started = run.start(home, cfg, child=children(SLEEPER))
    try:
        # start() has returned and closed its descriptor; the child holds the lock.
        assert started.process.poll() is None
        assert run.acquire_lock(home) is None
        assert os.getsid(started.process.pid) == started.process.pid
    finally:
        _stop(started)
    fd = run.acquire_lock(home)
    assert fd is not None
    os.close(fd)


def test_a_second_start_exits_4_naming_the_running_dream_and_changes_nothing(
    home, cfg, children, capsys
):
    started = run.start(home, cfg, child=children(SLEEPER))
    try:
        (home / "config.toml").write_text(
            f'[instructions]\nrepo = "{cfg["instructions"]["repo"]}"\n'
        )
        conn = store.open_store(home / "dreaming.db")
        before = (dump(conn), files(home))
        assert cli.main(["run"]) == SKIPPED
        assert started.run_id in capsys.readouterr().err
        assert (dump(conn), files(home)) == before
        conn.close()
    finally:
        _stop(started)


def test_a_model_call_subprocess_does_not_inherit_the_lock(
    home, cfg, children, tmp_path
):
    pidfile = tmp_path / "call.pid"
    child = children(f"""
        from dreaming.backends import run_process
        run_process(["sh", "-c", "echo $$ > {pidfile}; exec sleep 60"], timeout=120)
    """)
    started = run.start(home, cfg, child=child)
    call = _read_pid(pidfile)
    children.pids.append(call)
    _stop(started)
    # The call outlives the dream but never held its lock.
    assert _alive(call)
    fd = run.acquire_lock(home)
    assert fd is not None
    os.close(fd)


def test_a_resume_kills_survivors_of_the_recorded_process_group_first(
    home, cfg, children, tmp_path
):
    pidfile = tmp_path / "helper.pid"
    child = children(f"""
        import pathlib, subprocess, time
        helper = subprocess.Popen(["sleep", "60"])
        pathlib.Path("{pidfile}").write_text(str(helper.pid))
        time.sleep(60)
    """)
    started = run.start(home, cfg, child=child)
    helper = _read_pid(pidfile)
    children.pids.append(helper)
    _stop(started)
    assert _alive(helper)

    resumed = run.start(home, cfg, child=children(SLEEPER))
    try:
        assert resumed.resumed
        assert resumed.run_id == started.run_id
        assert _wait_for(lambda: not _alive(helper))
    finally:
        _stop(resumed)


def test_a_live_process_reusing_the_recorded_group_id_is_left_alone(
    home, cfg, children
):
    import subprocess

    unrelated = subprocess.Popen(["sleep", "60"], start_new_session=True)
    children.pids.append(unrelated.pid)
    _add(home, cfg, "dream-old", status="running", process_group=unrelated.pid)

    resumed = run.start(home, cfg, child=children(SLEEPER))
    try:
        assert resumed.run_id == "dream-old"
        assert unrelated.poll() is None
    finally:
        _stop(resumed)
        unrelated.kill()
        unrelated.wait()


# Starting and resuming


def test_a_new_run_records_its_requester_revision_snapshot_and_selection(
    home, cfg, children
):
    selection = {"project": "/work/repo", "reread": True}
    started = run.start(
        home,
        cfg,
        selection=selection,
        overrides={"budget": {"tokens": 5000}},
        requester="screw",
        child=children(SLEEPER),
    )
    try:
        assert not started.resumed
        assert started.run_id.startswith("dream-")
        conn = store.open_store(home / "dreaming.db")
        row = run_row(conn, started.run_id)
        assert row["status"] == "running"
        assert row["requester"] == "screw"
        assert row["revision"] == versions.revision()
        assert row["process_group"] == started.process.pid
        assert json.loads(row["selection"]) == selection
        assert json.loads(row["overrides"]) == {"budget": {"tokens": 5000}}
        assert json.loads(row["budget"])["tokens"] == 5000
        snapshot = home / "runs" / started.run_id / "snapshot"
        assert row["snapshot_path"] == str(snapshot)
        assert (snapshot / "instructions" / "lenses.toml").is_file()
        conn.close()
    finally:
        _stop(started)


def test_an_invalid_override_is_a_usage_error_that_starts_nothing(home, cfg):
    with pytest.raises(DreamingError) as excinfo:
        run.start(home, cfg, overrides={"tools": {}})
    assert excinfo.value.exit_code == USAGE_ERROR
    conn = store.open_store(home / "dreaming.db")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    conn.close()
    assert not (home / "runs").exists() or not any((home / "runs").iterdir())


def _interrupted(home, cfg, **fields):
    _add(
        home,
        cfg,
        "dream-1",
        status="interrupted",
        selection={"reread": True},
        overrides={"budget": {"tokens": 900}},
        **fields,
    )


def test_resume_keeps_selection_and_overrides_and_accepts_backend_choices(
    home, cfg, children
):
    _interrupted(home, cfg)
    resumed = run.start(
        home, cfg, overrides={"roles": {"reader": ["codex"]}}, child=children(SLEEPER)
    )
    try:
        assert (resumed.run_id, resumed.resumed) == ("dream-1", True)
        assert resumed.selection == {"reread": True}
        assert resumed.overrides == {
            "budget": {"tokens": 900},
            "roles": {"reader": ["codex"]},
        }
        conn = store.open_store(home / "dreaming.db")
        row = run_row(conn, "dream-1")
        assert row["status"] == "running"
        assert json.loads(row["overrides"])["roles"] == {"reader": ["codex"]}
        assert json.loads(row["selection"]) == {"reread": True}
        conn.close()
    finally:
        _stop(resumed)


@pytest.mark.parametrize(
    "request_",
    [
        {"overrides": {"budget": {"tokens": 10}}},
        {"overrides": {"fallback_on_usage_limit": True}},
        {"selection": {"project": "/elsewhere"}},
    ],
)
def test_resume_rejects_anything_but_backend_choices(home, cfg, request_):
    _interrupted(home, cfg)
    conn = store.open_store(home / "dreaming.db")
    before = dump(conn)
    with pytest.raises(DreamingError) as excinfo:
        run.start(home, cfg, **request_)
    assert excinfo.value.exit_code == USAGE_ERROR
    assert excinfo.value.code == "invalid_override"
    assert dump(conn) == before
    conn.close()


def test_a_run_started_on_another_revision_is_not_resumed_here(home, cfg):
    _interrupted(home, cfg, revision="0123abcd")
    with pytest.raises(DreamingError) as excinfo:
        run.start(home, cfg)
    assert "0123abcd" in excinfo.value.message
    assert excinfo.value.code == "wrong_revision"


@pytest.mark.parametrize("status", ["complete", "budget_exhausted", "partial"])
def test_finished_runs_are_not_resumed(home, cfg, children, status):
    _add(home, cfg, "dream-done", status=status)
    started = run.start(home, cfg, child=children(SLEEPER))
    try:
        assert not started.resumed
        assert started.run_id != "dream-done"
    finally:
        _stop(started)


def test_the_real_child_runs_the_dream_detached_and_exits_with_its_code(home, cfg):
    started = run.start(home, cfg)
    assert started.process.wait(timeout=60) == 0
    conn = store.open_store(home / "dreaming.db")
    row = run_row(conn, started.run_id)
    assert row["status"] == "complete"
    assert row["ended_at"] is not None
    conn.close()
    log = (home / "runs" / started.run_id / "run.log").read_text()
    assert "complete" in log


def test_dream_run_waits_for_the_exit_code_when_asked(home, cfg, capsys):
    (home / "config.toml").write_text(
        f'[instructions]\nrepo = "{cfg["instructions"]["repo"]}"\n'
    )
    assert cli.main(["run", "--wait", "--requester", "screw"]) == 0
    out = capsys.readouterr().out
    assert "started dream-" in out


# Exit codes


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("complete", 0),
        ("budget_exhausted", 0),
        ("partial", 5),
        ("interrupted", 1),
    ],
)
def test_exit_codes(status, code):
    assert run.exit_code(status) == code


def test_no_backend_left_saves_progress_and_ends_partial(db, home):
    run_id = add_run(db)
    clock = FakeClock()
    reports = []

    def before(role, prompt):
        if role == "reviewer":
            raise Partial("no backend left for the reviewer")

    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": CallPhase("reader"), "review": CallPhase("reviewer")},
        model=FakeModel(clock, before=before),
        clock=clock,
        planner=reads(2),
        report=lambda conn, run_id: reports.append(run_id),
    )
    status = runner.execute()
    assert status == "partial"
    assert run.exit_code(status) == PARTIAL
    assert reports == [run_id]
    row = run_row(db)
    assert row["status"] == "partial"
    assert "reviewer" in row["error"]
    states = {s.key: s.state for s in steps.load(db, run_id)}
    assert states == {"read:1": "done", "read:2": "done", "review": "pending"}


def test_an_unexpected_error_leaves_the_run_interrupted_with_exit_1(db, home):
    run_id = add_run(db)
    clock = FakeClock()

    class Broken(CallPhase):
        def apply(self, conn, step, answers):
            raise KeyError("missing field")

    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={"read": Broken("reader")},
        model=FakeModel(clock),
        clock=clock,
        planner=reads(1, review=False),
    )
    status = runner.execute()
    assert (status, run.exit_code(status)) == ("interrupted", 1)
    assert "missing field" in run_row(db)["error"]


def test_a_stop_request_is_honoured_between_steps(db, home):
    run_id = add_run(db)
    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        fake_config(),
        phases={},
        model=FakeModel(clock),
        clock=clock,
        planner=reads(2, review=False),
    )

    class StopAfterFirst(CallPhase):
        def apply(self, conn, step, answers):
            runner.stop()
            return super().apply(conn, step, answers)

    runner.phases = {"read": StopAfterFirst("reader")}
    assert runner.execute() == "interrupted"
    states = {s.key: s.state for s in steps.load(db, run_id)}
    assert states == {"read:1": "done", "read:2": "pending"}


def test_dream_run_with_an_invalid_config_exits_2(home, capsys):
    (home / "config.toml").write_text("[budget]\ntokens = -1\n")
    assert cli.main(["run"]) == USAGE_ERROR

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from dreaming.config import ConfigError, load_config
from dreaming.run import AlreadyRunning, Engine, RunSession, exit_code
from dreaming.steps import CallResult, Step, Steps
from dreaming.store import Store


@pytest.fixture
def run_config(tmp_path, instruction_repo):
    path = tmp_path / "config.toml"
    path.write_text(f'[instructions]\nrepository="{instruction_repo[0]}"\n')
    return load_config(path)


def test_resume_retains_snapshot_selection_budget_and_only_accepts_backends(tmp_path, run_config):
    home = tmp_path / "home"
    with RunSession(home) as session:
        record = session.prepare(
            run_config,
            revision="old",
            selection=["chosen"],
            overrides={"scope": "example"},
            steps=(Step("read:1", "Read"),),
        )
        original = record.id
        session.store.connection.execute("UPDATE runs SET status='interrupted'")
    with RunSession(home) as session:
        with pytest.raises(ConfigError, match="backend"):
            session.prepare(run_config, revision="new", overrides={"scope": "different"})
        record = session.prepare(
            run_config, revision="new", backend_overrides={"reader": ["codex"]}
        )
        assert record.id == original and record.resumed
        assert record.revision == "old"
        assert record.selection == ["chosen"]
        assert record.overrides == {"scope": "example"}
        assert record.config.roles["reader"] == ("codex",)
        assert record.config.budget == run_config.budget


@pytest.mark.parametrize(
    "status,expected",
    [
        ("complete", 0),
        ("budget_exhausted", 0),
        ("discarded", 0),
        ("partial", 5),
        ("interrupted", 1),
        ("config_error", 2),
        ("already_running", 4),
    ],
)
def test_exit_codes(status, expected):
    assert exit_code(status) == expected


def test_budget_exhaustion_finalizes_without_model_calls_and_adoption_finishes(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,budget) VALUES ('r','running',?)",
            (json.dumps({"max_tokens": 1}),),
        )
        steps = Steps(store, "r")
        steps.add(Step("read", "Read"), Step("adopt", "Adopt"))
        steps.start("read")
        ref = steps.begin_call("read", role="reader", backend="fake")
        steps.record_call(ref, CallResult("{}", 1, 1))
        applied, reports = [], []

        class Handler:
            def apply(self, conn, step, raw):
                applied.append(step.key)
                return ()

        engine = Engine(
            store,
            "r",
            {"Read": Handler(), "Adopt": Handler()},
            finalize=lambda s, r: reports.append(r),
        )
        assert engine.execute() == 0
        assert applied == ["read", "adopt"]
        assert reports == ["r"]
        assert (
            store.connection.execute("SELECT status FROM runs").fetchone()[0] == "budget_exhausted"
        )


def test_second_start_changes_no_rows_or_files(tmp_path, run_config):
    home = tmp_path / "home"
    with RunSession(home) as first:
        record = first.prepare(run_config, revision="one")
        rows = list(first.store.connection.iterdump())
        lock_stat = (home / "lock").stat()
        with pytest.raises(AlreadyRunning, match=record.id):
            with RunSession(home):
                pytest.fail("acquired an already-held lock")
        assert list(first.store.connection.iterdump()) == rows
        assert (home / "lock").stat() == lock_stat
        assert (home / "lock").read_bytes() == b""


def wait_for(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        time.sleep(0.02)
    pytest.fail(f"worker did not create {path}")


def test_detached_child_holds_lock_after_invoker_exits_and_grandchild_does_not(
    tmp_path, run_config
):
    home = tmp_path / "home"
    module = tmp_path / "fake_worker.py"
    module.write_text("""import os, subprocess, sys, time
def work(store, run_id):
    home = store.path.parent
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             close_fds=True, start_new_session=True)
    (home / "child").write_text(str(child.pid))
    (home / "ready").write_text(str(os.getpid()))
    time.sleep(60)
""")
    parent = """from pathlib import Path
from dreaming.config import load_config
from dreaming.run import RunSession
import sys
with RunSession(Path(sys.argv[1])) as s:
    r = s.prepare(load_config(Path(sys.argv[2])), revision="old")
    s.detach("fake_worker:work", r.id)
"""
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    subprocess.run(
        [sys.executable, "-c", parent, str(home), str(run_config.source)],
        env=env,
        check=True,
        capture_output=True,
        timeout=15,
    )
    worker_pid = int(wait_for(home / "ready"))
    child_pid = int(wait_for(home / "child"))
    try:
        with pytest.raises(AlreadyRunning):
            with RunSession(home):
                pass
        os.kill(worker_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            try:
                with RunSession(home) as session:
                    session.prepare(run_config, revision="new")
                break
            except AlreadyRunning:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        os.kill(child_pid, 0)  # Still alive, but cannot hold the worker's descriptor.
    finally:
        for pid in (worker_pid, child_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_resume_kills_recorded_process_group(tmp_path, run_config):
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        close_fds=True,
    )
    try:
        with RunSession(tmp_path / "home") as session:
            record = session.prepare(run_config, revision="old")
            session.store.connection.execute(
                "UPDATE runs SET process_group=? WHERE id=?", (survivor.pid, record.id)
            )
        with RunSession(tmp_path / "home") as session:
            assert session.prepare(run_config, revision="new").resumed
        assert survivor.wait(timeout=5) == -signal.SIGKILL
    finally:
        if survivor.poll() is None:
            survivor.kill()
        survivor.wait()


def test_interrupt_finishes_current_step_and_leaves_next_pending(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        steps = Steps(store, "r")
        steps.add(Step("check", "Adopt"), Step("later", "Adopt", ("check",)))

        class Check:
            def apply(self, conn, step, raw):
                os.kill(os.getpid(), signal.SIGTERM)
                conn.execute("UPDATE runs SET notice='check finished'")
                return ()

        previous = signal.getsignal(signal.SIGTERM)
        assert Engine(store, "r", {"Adopt": Check()}).execute() == 1
        assert [s["state"] for s in steps.list()] == ["done", "pending"]
        assert signal.getsignal(signal.SIGTERM) == previous


def test_session_limit_stops_new_reads_but_preserves_review_capacity(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,budget) VALUES ('r','running',?)",
            (json.dumps({"max_sessions": 1}),),
        )
        steps = Steps(store, "r")
        steps.add(
            Step("read:1", "Read"),
            Step("read:2", "Read"),
            Step("review", "Review", ("read:1", "read:2")),
        )
        called = []

        class Handler:
            def prepare(self, step):
                return {"role": "reader" if step.phase == "Read" else "reviewer", "backend": "fake"}

            def call(self, step):
                called.append(step.key)
                return CallResult("{}", 1, 1)

            def apply(self, conn, step, raw):
                return ()

        assert Engine(store, "r", {"Read": Handler(), "Review": Handler()}).execute() == 0
        assert called == ["read:1", "review"]
        assert (
            store.connection.execute("SELECT status FROM runs").fetchone()[0] == "budget_exhausted"
        )


def test_standalone_interrupted_probe_does_not_become_a_resumable_dream(tmp_path, run_config):
    with RunSession(tmp_path / "home") as session:
        session.store.connection.execute(
            "INSERT INTO runs(id,status,revision) VALUES ('probe-old','interrupted','0.1.0')"
        )
        assert not session.prepare(run_config, revision="new").resumed

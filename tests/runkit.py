"""A fake model, a fake clock and small phases for run-engine tests."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dreaming import config, run, steps
from dreaming.backends import CallResult

START = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
SCHEMA = {"type": "object", "required": ["value"]}
RESULTS = "CREATE TABLE IF NOT EXISTS results (step TEXT PRIMARY KEY, answers TEXT)"


class Crash(BaseException):
    """The process dying: nothing handles it and open transactions roll back."""


class FakeClock:
    """Time moves only when a call takes time or the run sleeps."""

    def __init__(self):
        self.seconds = 0.0
        self.on_sleep = None

    def monotonic(self):
        return self.seconds

    def now(self):
        return START + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        if self.on_sleep:
            self.on_sleep(seconds)
        self.seconds += seconds


class FakeModel:
    """Answers `{"value": n}`, where n counts the calls it has answered, so a call
    made again gets a different answer."""

    name = "fake"
    context_tokens = 100_000
    release_date = ""
    accepts_session_id = False

    def __init__(self, clock=None, *, tokens=(100, 20), seconds=0.0, before=None):
        self.clock = clock
        self.tokens = tokens
        self.seconds = seconds
        self.before = before
        self.answered = 0
        self.prompts = []

    def call(self, role, prompt, schema, tools, session_id=None):
        if self.before:
            self.before(role, prompt)
        self.prompts.append((role, prompt))
        if self.clock:
            self.clock.seconds += self.seconds
        value = self.answered
        self.answered += 1
        return CallResult(
            {"value": value},
            *self.tokens,
            session_ref=f"fake-session-{value}",
            model="fake-model",
        )


def fake_config(**budget):
    return config.build_config(
        {
            "backends": {"fake": {"harness": "claude", "context_tokens": 100_000}},
            "roles": {role: {"backends": ["fake"]} for role in config.ROLES},
            "budget": budget,
        }
    )


def tick(hook):
    if hook:
        hook()


class CallPhase:
    """Each step asks the model once per part and stores the answers in `results`."""

    def __init__(self, role, *, parts=("",), follow=None, hook=None):
        self.role = role
        self.parts = parts
        self.follow = follow
        self.hook = hook

    def prepare(self, ctx, step):
        tick(self.hook)
        return [
            ctx.call(self.role, f"{step.key} {part}", SCHEMA, part=part)["value"]
            for part in self.parts
        ]

    def apply(self, conn, step, answers):
        tick(self.hook)
        conn.execute(RESULTS)
        conn.execute(
            "INSERT INTO results VALUES (?, ?)", (step.key, json.dumps(answers))
        )
        follow_up = self.follow(step, answers) if self.follow else ()
        return run.Outcome(follow_up=follow_up)


class LocalPhase:
    """A step that makes no model call, as adoption does."""

    def __init__(self, hook=None):
        self.hook = hook

    def prepare(self, ctx, step):
        tick(self.hook)

    def apply(self, conn, step, prepared):
        tick(self.hook)
        conn.execute(RESULTS)
        conn.execute("INSERT INTO results VALUES (?, 'local')", (step.key,))
        return run.Outcome()


def reads(count, *, review=True):
    """A planner: `count` Read steps and a Review step depending on all of them."""
    keys = [f"read:{i}" for i in range(1, count + 1)]
    planned = [steps.NewStep(key, "read") for key in keys]
    if review:
        planned.append(steps.NewStep("review", "review", depends_on=tuple(keys)))
    return lambda ctx: planned


def add_run(db, run_id="dream-test", cfg=None, **fields):
    run.create_run(db, run_id, cfg or fake_config(), **fields)
    return run_id


def make_runner(db, home, run_id, cfg, *, phases, model, clock, **kwargs):
    return run.Runner(
        db,
        home,
        run_id,
        cfg,
        phases=phases,
        backends={"fake": model},
        clock=clock,
        **kwargs,
    )


def run_row(db, run_id="dream-test"):
    return db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def results(db):
    db.execute(RESULTS)
    return {row[0]: row[1] for row in db.execute("SELECT step, answers FROM results")}


def step_states(db, run_id="dream-test"):
    return {s.key: s.state for s in steps.load(db, run_id)}


def dump(db):
    """Every row of the store, for checking that nothing changed."""
    return list(db.iterdump())


def files(root: Path):
    """Every path under `root`, without SQLite's transient journal files."""
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if not p.name.endswith(("-wal", "-shm"))
    )


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def instruction_repo(root: Path) -> Path:
    """A repository holding a committed instruction set on `main`."""
    repo = root / "instruction-repo"
    (repo / "instructions").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "instructions" / "lenses.toml").write_text(
        '[[lens]]\nid = "prefs"\nversion = 1\nlook_for = "preferences"\n'
        'may_propose = ["knowledge"]\n'
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "instructions")
    return repo

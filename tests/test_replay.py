import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from dreaming.backends import Dispatcher
from dreaming.config import ROLES, BackendConfig, load_config
from dreaming.knowledge import forget_session
from dreaming.roles import RoleSet
from dreaming.steps import CallResult, Step, Steps
from dreaming.store import Store

INSTRUCTIONS = Path(__file__).resolve().parents[1] / "instructions"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


class ReplayBackend:
    supports_tools = False

    def __init__(self):
        self.calls = []
        self.baseline = "recurs"
        self.changed = "resolved"
        self.after_call = None
        self.failure = False
        self.same_output = False

    def call(self, role, prompt, schema, tools):
        assert not tools
        self.calls.append((role, prompt))
        if self.failure:
            return CallResult("", 3, 1, outcome="transport_failure", error="offline")
        if role == "reviewer" and "baseline" in schema.get("properties", {}):
            output = {
                "baseline": self.baseline,
                "changed": self.changed,
                "reason": "Compared outputs",
            }
        elif role == "reader":
            revised = (
                "Clear explicit user preferences" in prompt or "Prefer concrete examples." in prompt
            )
            observation = {
                "source": "claude",
                "session_id": "s0",
                "entry_id": "0",
                "source_location": "entry:0",
                "representation_version": 1,
                "quote": "A concrete example",
                "kind": "preference",
                "modality": "asserted",
                "outcome": "unknown",
                "outcome_evidence": [],
                "lens_id": "preferences",
                "lens_version": "1",
                "pattern_key": "examples",
                "subject": "user",
            }
            output = {"observations": [observation] if revised else []}
        else:
            revised = "Use concrete examples." in prompt and not self.same_output
            output = {"response": "Concrete example" if revised else "Vague example"}
        if self.after_call:
            self.after_call(role)
        return CallResult(json.dumps(output), 3, 2, model="fake-model")


@pytest.fixture
def replay_env(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "rule.md").write_text("# Rule\nUse vague examples.\nKeep this line.\n")
    shutil.copytree(INSTRUCTIONS, repo / "instructions")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    (repo / "rule.md").write_text("# Rule\nUse concrete examples.\nKeep this line.\n")
    git(repo, "commit", "-am", "Change the rule")
    commit = git(repo, "rev-parse", "HEAD")
    config = load_config(tmp_path / "config.toml")
    config = replace(
        config,
        backends={"fake": BackendConfig(context_tokens=30000)},
        roles={role: ("fake",) for role in ROLES},
        repositories={"target": {"path": str(repo), "default_branch": "main"}},
        thresholds=replace(config.thresholds, replay_min_sessions=2, replay_max_sessions=3),
    )
    with Store(tmp_path / "home") as store:
        store.connection.execute("INSERT INTO runs(id,status) VALUES ('r','running')")
        store.connection.execute("INSERT INTO patterns(id,key) VALUES ('pattern','examples')")
        for index in range(2):
            document = {
                "schema_version": 1,
                "kind": "session",
                "session": {"source": "claude", "id": f"s{index}"},
                "entries": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": f"Explain with examples {index}"}],
                    }
                ],
                "warnings": [],
            }
            path = tmp_path / f"session-{index}.json"
            path.write_text(json.dumps(document))
            store.connection.execute(
                "INSERT INTO sessions_seen(source,session_id,document_path) VALUES ('claude',?,?)",
                (f"s{index}", str(path)),
            )
            store.connection.execute(
                "INSERT INTO observations(id,run_id,source,session_id,entry_id,source_location,"
                "representation_version,redacted_quote,kind,modality,outcome,lens_id,lens_version,"
                "pattern_id,backend) VALUES (?,'r','claude',?,'entry:0','entry:0',1,"
                "'Vague examples',"
                "'friction','asserted','unknown','preferences','1','pattern','fake')",
                (f"e{index}", f"s{index}"),
            )
        edits = [
            {"path": "rule.md", "before": "Use vague examples.", "after": "Use concrete examples."}
        ]
        store.connection.execute(
            "INSERT INTO changes(id,run_id,pattern_id,repo,branch,commit_sha,evidence,authoring_id,"
            "edits) VALUES ('change','r','pattern',?,'dream/r',?,'[\"e0\",\"e1\"]','author',?)",
            (str(repo), commit, json.dumps(edits)),
        )
        backend = ReplayBackend()
        dispatcher = Dispatcher(store, "r", config, {"fake": backend})
        roles = RoleSet(repo / "instructions", config)
        yield store, config, repo, dispatcher, roles, backend


def change(store):
    cursor = store.connection.execute("SELECT * FROM changes WHERE id='change'")
    return dict(zip((c[0] for c in cursor.description), cursor.fetchone(), strict=True))


def gate(env):
    from dreaming.replay import ReplayGate

    store, config, _, dispatcher, roles, _ = env
    return ReplayGate(store, "r", config, dispatcher, roles)


def test_replay_saves_both_outputs_and_judgements_once_per_authoring(replay_env):
    store, _, _, dispatcher, _, backend = replay_env
    replay = gate(replay_env)
    assert replay.check(change(store)) is None
    assert len(backend.calls) == 6
    assert [role for role, _ in backend.calls] == ["replayer", "replayer", "reviewer"] * 2
    assert store.connection.execute("SELECT count(*) FROM replays").fetchone()[0] == 2
    assert store.connection.execute("SELECT status FROM replay_batches").fetchone()[0] == "passed"
    assert dispatcher.budget.tokens_used == 30
    assert (
        store.connection.execute(
            "SELECT count(*) FROM calls WHERE raw_output IS NOT NULL AND outcome='success'"
        ).fetchone()[0]
        == 6
    )
    assert replay.check(change(store)) is None
    assert len(backend.calls) == 6
    assert all(state == "done" for (state,) in store.connection.execute("SELECT state FROM steps"))


@pytest.mark.parametrize("outcome", ["recurs", "unclear"])
def test_noop_and_unclear_changes_are_held(replay_env, outcome):
    store, _, _, _, _, backend = replay_env
    backend.changed = outcome
    assert gate(replay_env).check(change(store)) == "replay_recurrence"
    assert store.connection.execute("SELECT status FROM replay_batches").fetchone()[0] == "held"
    assert gate(replay_env).check(change(store)) == "replay_recurrence"
    assert len(backend.calls) == 6


def test_identical_outputs_cannot_be_judged_as_resolving_the_baseline(replay_env):
    store, _, _, _, _, backend = replay_env
    backend.same_output = True
    assert gate(replay_env).check(change(store)) == "replay_recurrence"


@pytest.mark.parametrize("baseline", ["resolved", "unclear"])
def test_only_sessions_with_a_recurring_baseline_count(replay_env, baseline):
    store, _, _, _, _, backend = replay_env
    backend.baseline = baseline
    assert gate(replay_env).check(change(store)) == "replay_insufficient_sessions"
    assert store.connection.execute("SELECT sum(baseline_recurs) FROM replays").fetchone()[0] == 0


@pytest.mark.parametrize("broken", ["truncated", "assistant_first", "empty", "unattributed"])
def test_incomplete_opening_prompts_are_never_replayed(replay_env, broken):
    store, _, _, _, _, backend = replay_env
    path = Path(
        store.connection.execute(
            "SELECT document_path FROM sessions_seen WHERE session_id='s0'"
        ).fetchone()[0]
    )
    document = json.loads(path.read_text())
    if broken == "truncated":
        document["entries"][0]["content"][0]["truncated"] = True
    elif broken == "assistant_first":
        document["entries"][0]["role"] = "assistant"
    elif broken == "unattributed":
        document["entries"][0]["unattributed"] = True
    else:
        document["entries"] = []
    path.write_text(json.dumps(document))
    assert gate(replay_env).check(change(store)) == "replay_insufficient_sessions"
    assert backend.calls == []


def test_forgotten_evidence_holds_before_replay_and_after_cached_pass(replay_env):
    store, _, _, _, _, backend = replay_env
    assert gate(replay_env).check(change(store)) is None
    forget_session(store, "claude", "s0")
    assert gate(replay_env).check(change(store)) == "replay_evidence_unavailable"
    assert len(backend.calls) == 6


def test_forgetting_between_calls_stops_before_the_next_replay(replay_env):
    store, _, _, _, _, backend = replay_env
    backend.after_call = lambda role: forget_session(store, "claude", "s0")
    assert gate(replay_env).check(change(store)) == "replay_evidence_unavailable"
    assert len(backend.calls) == 1


def test_file_finding_only_and_code_changes_skip_replay(replay_env):
    store, _, repo, _, _, backend = replay_env
    store.connection.execute(
        "INSERT INTO file_findings(id,run_id,repo,revision,path,location,lens_id,finding) "
        "VALUES ('file','r',?,'revision','rule.md','file','preferences','Broken reference')",
        (str(repo),),
    )
    store.connection.execute("UPDATE changes SET evidence='[\"file\"]'")
    assert gate(replay_env).check(change(store)) is None
    store.connection.execute(
        'UPDATE changes SET evidence=\'["e0","e1"]\',edits=?',
        (json.dumps([{"path": "code.py", "before": "old", "after": "new"}]),),
    )
    assert gate(replay_env).check(change(store)) is None
    assert not backend.calls


def test_replay_budget_is_checked_before_any_call(replay_env):
    store, _, _, dispatcher, _, backend = replay_env
    dispatcher.budget.limits = replace(dispatcher.budget.limits, max_tokens=1)
    assert gate(replay_env).check(change(store)) == "replay_budget"
    assert not backend.calls


def test_replay_never_switches_backend_after_failure(replay_env):
    store, config, repo, dispatcher, roles, backend = replay_env
    fallback = ReplayBackend()
    config = replace(
        config,
        backends={**config.backends, "fallback": BackendConfig(context_tokens=30000)},
        roles={role: ("fake", "fallback") for role in ROLES},
    )
    dispatcher.config = config
    dispatcher.backends["fallback"] = fallback
    backend.failure = True
    from dreaming.replay import ReplayGate

    assert ReplayGate(store, "r", config, dispatcher, roles).check(change(store)) == (
        "replay_backend_unavailable"
    )
    assert not fallback.calls
    assert dispatcher.config is config


def test_indeterminate_replay_call_is_held_without_calling_again(replay_env):
    store, _, _, _, _, backend = replay_env
    original = backend.call

    def crash(*args):
        raise RuntimeError("worker died during response")

    backend.call = crash
    with pytest.raises(RuntimeError, match="worker died"):
        gate(replay_env).check(change(store))
    backend.call = original
    assert gate(replay_env).check(change(store)) == "replay_indeterminate_call"
    assert not backend.calls


def test_saved_successful_replay_outputs_are_reused_after_a_crash(replay_env):
    store, _, _, _, _, backend = replay_env
    replay = gate(replay_env)
    original = replay._call

    def crash_after_response(key, request, sessions):
        original(key, request, sessions)
        raise RuntimeError("Response was saved before worker died")

    replay._call = crash_after_response
    with pytest.raises(RuntimeError, match="saved before"):
        replay.check(change(store))
    assert len(backend.calls) == 1
    assert gate(replay_env).check(change(store)) is None
    assert len(backend.calls) == 6
    assert {model for (model,) in store.connection.execute("SELECT model FROM replays")} == {
        "fake-model"
    }


def test_all_saved_replay_outputs_apply_even_after_budget_is_exhausted(replay_env):
    store, _, _, dispatcher, _, backend = replay_env
    replay = gate(replay_env)
    original = replay._call

    def crash_after_last_judge(key, request, sessions):
        result = original(key, request, sessions)
        if key.endswith(":judge") and len(backend.calls) == 6:
            raise RuntimeError("Last judgement saved before worker died")
        return result

    replay._call = crash_after_last_judge
    with pytest.raises(RuntimeError, match="Last judgement"):
        replay.check(change(store))
    dispatcher.budget.limits = replace(dispatcher.budget.limits, max_tokens=1)
    assert gate(replay_env).check(change(store)) is None
    assert len(backend.calls) == 6
    assert store.connection.execute("SELECT count(*) FROM replays").fetchone()[0] == 2


def test_replayer_and_reviewer_can_have_separate_backend_lists(replay_env):
    from dreaming.replay import ReplayGate

    store, config, _, dispatcher, roles, backend = replay_env
    judge = ReplayBackend()
    config = replace(
        config,
        backends={**config.backends, "judge": BackendConfig(context_tokens=30000)},
        roles={**config.roles, "reviewer": ("judge",)},
    )
    dispatcher.config = config
    dispatcher.backends["judge"] = judge
    assert ReplayGate(store, "r", config, dispatcher, roles).check(change(store)) is None
    assert [role for role, _ in backend.calls] == ["replayer"] * 4
    assert [role for role, _ in judge.calls] == ["reviewer"] * 2


@pytest.mark.parametrize("kind", ["lens", "role"])
@pytest.mark.parametrize("complete", [True, False, "forgotten_lineage", "forget_after_pass"])
def test_lens_and_role_changes_rerun_the_affected_role_on_stored_inputs(replay_env, kind, complete):
    store, _, repo, dispatcher, roles, backend = replay_env
    path = "instructions/lenses.toml" if kind == "lens" else "instructions/roles/reader.md"
    original = (repo / path).read_text()
    revised = original.replace("Explicit user preferences", "Clear explicit user preferences", 1)
    if revised == original:
        revised = original + "\nPrefer concrete examples.\n"
    (repo / path).write_text(revised)
    git(repo, "add", path)
    git(repo, "commit", "-m", "Update the instruction")
    store.connection.execute(
        "UPDATE changes SET commit_sha=?,edits=?",
        (
            git(repo, "rev-parse", "HEAD"),
            json.dumps([{"path": path, "before": original, "after": revised}]),
        ),
    )
    # A prior reader call is input evidence, not another model call by this gate.
    if complete in {"forgotten_lineage", "forget_after_pass"}:
        store.connection.execute(
            "INSERT INTO sessions_seen(source,session_id) VALUES ('claude','private')"
        )
    for index in range(2):
        request = roles.build(
            "reader",
            sessions=[{"source": "claude", "id": f"s{index}"}],
            lenses=[vars(lens) for lens in roles.lenses],
        )
        key = f"historical-{index}"
        Steps(store, "r").add(Step(key, "Read"))
        Steps(store, "r").start(key)
        call = Steps(store, "r").begin_call(
            key,
            role="reader",
            backend="fake",
            input_ref=json.dumps(request.metadata()) if complete else None,
            sessions=(("claude", f"s{index}"),)
            + (
                (("claude", "private"),)
                if complete in {"forgotten_lineage", "forget_after_pass"}
                else ()
            ),
        )
        Steps(store, "r").record_call(call, CallResult('{"observations":[]}', tokens_in=3000))
        Steps(store, "r").apply(key, "{}", lambda conn, raw: ())
    dispatcher.budget.limits = replace(dispatcher.budget.limits, max_sessions=0, reserve_share=0.99)
    if complete == "forgotten_lineage":
        forget_session(store, "claude", "private")
    if complete is False or complete == "forgotten_lineage":
        assert gate(replay_env).check(change(store)) == "replay_insufficient_sessions"
        assert not backend.calls
        return
    assert gate(replay_env).check(change(store)) is None
    assert [role for role, _ in backend.calls] == ["reader", "reader", "reviewer"] * 2
    changed = [prompt for role, prompt in backend.calls if role == "reader"]
    assert changed[0] != changed[1]
    if complete == "forget_after_pass":
        forget_session(store, "claude", "private")
        assert gate(replay_env).check(change(store)) == "replay_insufficient_sessions"
        assert len(backend.calls) == 6

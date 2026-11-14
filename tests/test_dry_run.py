"""The dry run and the readable plan (1.7, 5.5)."""

import json
import shutil
import subprocess

import pytest
from runkit import add_run, dump, files
from sessiondocs import NOW, StubApsis, add_seen, days_ago, make_doc

from dreaming import cli, config, plan, run, steps
from dreaming.backends import probe
from dreaming.steps import NewStep


@pytest.fixture
def cfg():
    return config.build_config(
        {
            "budget": {"sessions": 10, "tokens": 50_000},
            "roles": {"reader": {"backends": ["claude", "codex"]}},
        }
    )


def _apsis():
    return StubApsis(
        make_doc("claude", "newest", days_ago(1)),
        make_doc("codex", "older", days_ago(3)),
    )


def _dry_run(home, cfg, **kwargs):
    return run.dry_run(home, cfg, apsis=_apsis(), now=NOW, **kwargs)


def test_dry_run_lists_sessions_backends_per_role_and_budget(home, cfg, fakes):
    probe.probe_backend(home, cfg, "claude")
    result = _dry_run(home, cfg)
    assert [(s["source"], s["id"]) for s in result["sessions"]] == [
        ("claude", "newest"),
        ("codex", "older"),
    ]
    assert result["sessions"][0]["entries"] == 2
    reader = result["roles"]["reader"]
    assert [(b["backend"], b["status"]) for b in reader] == [
        ("claude", "cached"),
        ("codex", "unprobed"),
    ]
    assert reader[0]["usable"] and not reader[1]["usable"]
    assert [b["backend"] for b in result["roles"]["reviewer"]] == ["claude"]
    assert result["budget"]["tokens"] == 50_000
    assert result["resumes"] is None


def test_dry_run_applies_the_selection_and_overrides(home, cfg, fakes):
    result = _dry_run(
        home,
        cfg,
        selection={"since": days_ago(2)},
        overrides={"budget": {"tokens": 7}, "roles": {"reader": ["codex"]}},
    )
    assert [s["id"] for s in result["sessions"]] == ["newest"]
    assert result["budget"]["tokens"] == 7
    assert [b["backend"] for b in result["roles"]["reader"]] == ["codex"]


def test_an_unavailable_backend_is_reported_with_its_reason(
    home, cfg, fakes, monkeypatch
):
    monkeypatch.setenv("PATH", "/nonexistent")
    (claude,) = _dry_run(home, cfg)["roles"]["reviewer"]
    assert claude["status"] == "unavailable"
    assert "claude" in claude["reason"]


def test_dry_run_calls_no_model_and_changes_no_file_or_row(db, home, cfg, fakes):
    probe.probe_backend(home, cfg, "claude")
    add_run(db, "dream-earlier", status="complete")
    add_seen(db, "claude", "older", entries_read=1)
    before = (dump(db), files(home))
    calls_before = len(fakes.calls())

    result = _dry_run(home, cfg)
    assert result["sessions"]
    assert (dump(db), files(home)) == before
    assert all(c["argv"] == ["--version"] for c in fakes.calls()[calls_before:])


def test_dry_run_in_a_fresh_home_creates_nothing(isolated_home, cfg, fakes):
    home = config.dreaming_home()
    result = _dry_run(home, cfg)
    assert len(result["sessions"]) == 2
    assert not home.exists()


def test_dry_run_names_the_unfinished_dream_a_start_would_resume(db, home, cfg, fakes):
    add_run(db, "dream-1", status="interrupted")
    assert _dry_run(home, cfg)["resumes"] == "dream-1"


def test_dream_run_dry_run_prints_the_plan(home, fakes, capsys):
    assert cli.main(["run", "--dry-run", "--tokens", "1234"]) == 0
    out = capsys.readouterr().out
    assert "no model" in out
    assert "1234 tokens" in out
    assert "reader: claude (unprobed)" in out
    assert not (home / "dreaming.db").exists()

    assert cli.main(["run", "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"resumes", "sessions", "skipped", "roles", "budget"}
    assert result["roles"]["reader"][0]["status"] == "unprobed"


# The plan view

STEPS = [
    (NewStep("read:1", "read"), "done", None),
    (NewStep("read:2", "read"), "failed", "schema failed twice"),
    (NewStep("read:3", "read"), "skipped", "token budget reached\n(9 of 9)"),
    (NewStep("review", "review", ("read:1", "read:2", "read:3")), "running", None),
    (NewStep("research:ab12", "research", ("review",)), "pending", None),
]


def _planned(db):
    run_id = add_run(db, "dream-plan")
    steps.add(db, run_id, [step for step, _, _ in STEPS])
    for step, state, reason in STEPS:
        steps.finish(db, run_id, step.key, state, reason)
    return run_id


def test_the_plan_renders_steps_by_phase_in_rune_format(db, home):
    run_id = _planned(db)
    text = plan.render(run_id, steps.load(db, run_id))
    assert text.splitlines()[:4] == ["# Dream dream-plan", "", "## Read", ""]
    assert "- [x] 1. read:1" in text
    assert "- [x] 2. read:2 (failed)" in text
    assert "  - Reason: token budget reached (9 of 9)" in text
    assert "- [-] 4. review" in text
    assert "  - Depends on: read:1, read:2, read:3" in text
    assert "- [ ] 5. research:ab12" in text


@pytest.mark.skipif(shutil.which("rune") is None, reason="rune is not installed")
def test_rune_parses_the_plan_written_by_dream_status(db, home, capsys):
    run_id = _planned(db)
    assert cli.main(["status", run_id, "--plan"]) == 0
    path = home / "runs" / run_id / plan.PLAN_NAME
    assert str(path) in capsys.readouterr().out

    listed = subprocess.run(
        ["rune", "list", str(path), "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    tasks = json.loads(listed.stdout)["Tasks"]
    # rune statuses: 0 pending, 1 in progress, 2 completed
    assert [(t["Phase"], t["Status"]) for t in tasks] == [
        ("Read", 2),
        ("Read", 2),
        ("Read", 2),
        ("Review", 1),
        ("Research", 0),
    ]


def test_the_plan_is_never_read_back(db, home):
    run_id = _planned(db)
    path = plan.write(home, db, run_id)
    before = dump(db)
    path.write_text(path.read_text().replace("[ ]", "[x]") + "\n- [ ] 9. extra\n")
    rewritten = plan.write(home, db, run_id)
    assert rewritten == path
    assert "extra" not in path.read_text()
    assert "- [ ] 5. research:ab12" in path.read_text()
    assert dump(db) == before


def test_dream_status_shows_the_latest_run(db, home, capsys):
    run_id = _planned(db)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert run_id in out and "running" in out
    assert "1 done" in out and "1 pending" in out


def test_dream_status_of_an_unknown_run_is_not_found(db, home, capsys):
    assert cli.main(["status", "dream-nope"]) == 2
    assert "dream-nope" in capsys.readouterr().err

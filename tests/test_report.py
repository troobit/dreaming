import html
import json
import logging
import re
import subprocess
from dataclasses import replace

import pytest

from dreaming.config import ConfigError, load_config
from dreaming.run import Engine
from dreaming.store import Store


@pytest.fixture
def report_store(tmp_path):
    with Store(tmp_path / "home") as store:
        store.connection.execute(
            "INSERT INTO runs(id,status,revision,started_at,statistics,budget) "
            "VALUES ('dream-one','complete','revision-one','2026-11-15T00:00:00Z',?,?)",
            (
                json.dumps({"observations_accepted": 4, "observations_rejected": 2}),
                json.dumps({"tokens": 100, "active_seconds": 12, "max_tokens": 1000}),
            ),
        )
        yield store


def test_report_distinguishes_retained_knowledge_learning_and_empty_changes(report_store):
    from dreaming.report import render_report

    conn = report_store.connection
    conn.execute(
        "INSERT INTO knowledge_items(id,kind,scope,status,summary,confidence,"
        "confidence_basis,actor) "
        "VALUES ('knowledge-one','preference','{}','active','Keep examples short','high',"
        "'Explicit user evidence','dream')"
    )
    conn.execute(
        "INSERT INTO knowledge_revisions(item_id,version,kind,scope,status,summary,confidence,"
        "confidence_basis,actor,run_id) VALUES ('knowledge-one',1,'preference','{}','contested',"
        "'Keep examples short','medium','Conflicting evidence','dream','dream-one')"
    )
    conn.execute(
        "INSERT INTO learning(id,run_id,scope,evidence,next_step,outcome,disposition,reason) "
        "VALUES ('learn-one','dream-one','{}','[]','Practise joins','Understand joins','dismissed',"
        "'Already familiar')"
    )
    page = render_report(report_store, "dream-one")
    for expected in (
        "Retained knowledge revisions",
        "Keep examples short",
        "contested",
        "Conflicting evidence",
        "Learning recommendations",
        "Practise joins",
        "dismissed",
        "Already familiar",
        "No repository changes.",
        "No effect measurements",
        "No tooling inventory",
    ):
        assert expected in page
    assert "No result" not in page
    assert "<style>" in page and 'class="top-bar"' in page
    assert 'src="http' not in page


@pytest.mark.parametrize("status", ["complete", "partial", "budget_exhausted", "interrupted"])
def test_engine_exit_writes_report_for_every_outcome(report_store, status):
    from dreaming.report import write_report

    report_store.connection.execute("UPDATE runs SET status=?", (status,))
    path = write_report(report_store, "dream-one")
    assert path == report_store.path.parent / "reports" / "dream-one.html"
    assert status in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_engine_exception_still_finalizes_report(report_store):
    from dreaming.report import write_report
    from dreaming.steps import Step, Steps

    report_store.connection.execute("UPDATE runs SET status='running'")
    Steps(report_store, "dream-one").add(Step("failing", "Adopt"))

    class Failing:
        def apply(self, conn, step, raw):
            raise RuntimeError("A check failed")

    with pytest.raises(RuntimeError, match="check failed"):
        Engine(
            report_store,
            "dream-one",
            {"Adopt": Failing()},
            finalize=write_report,
        ).execute()
    page = (report_store.path.parent / "reports" / "dream-one.html").read_text()
    assert "interrupted" in page and "A check failed" in page


@pytest.mark.parametrize(
    "requester,enabled,expected", [("user", True, 1), ("user", False, 0), ("scheduler", True, 0)]
)
def test_browser_is_opened_only_when_enabled_for_user_runs(
    report_store, monkeypatch, requester, enabled, expected
):
    from dreaming.report import write_report

    report_store.connection.execute("UPDATE runs SET requester=?", (requester,))
    opened = []
    monkeypatch.setattr("dreaming.report.webbrowser.open", lambda uri: opened.append(uri) or True)
    path = write_report(report_store, "dream-one", open_browser=enabled)
    assert opened == [path.as_uri()] * expected


@pytest.mark.parametrize("raises", [True, False])
def test_browser_failure_is_logged_without_failing_report(
    report_store, monkeypatch, caplog, raises
):
    from dreaming.report import write_report

    def unavailable(uri):
        if raises:
            raise OSError("browser missing")
        return False

    monkeypatch.setattr("dreaming.report.webbrowser.open", unavailable)
    with caplog.at_level(logging.WARNING):
        path = write_report(report_store, "dream-one", open_browser=True)
    assert path.exists() and "Could not open" in caplog.text


def test_report_escapes_fetched_titles_and_only_links_http_sources(report_store):
    from dreaming.report import render_report

    sources = [
        {"title": '<img src=x onerror="alert(1)">', "link": "https://example.invalid/?x=1&y=2"},
        {"title": "Unsafe source", "link": "javascript:alert(1)"},
        {"title": "Local file", "url": "file:///private/names"},
    ]
    report_store.connection.execute(
        "INSERT INTO currency_findings(id,run_id,target,claim,sources,status) "
        "VALUES ('currency-one','dream-one','instructions','Update the guidance',?,'open')",
        (json.dumps(sources),),
    )
    page = render_report(report_store, "dream-one")
    assert "&lt;img src=x onerror=&quot;" in page
    assert "<img src=x" not in page
    assert 'href="https://example.invalid/?x=1&amp;y=2"' in page
    assert 'href="javascript:' not in page and 'href="file:' not in page
    assert "Unsafe source" in page and "Local file" in page


def test_report_shows_deferred_reasons_forgotten_sessions_and_previous_branches(report_store):
    from dreaming.report import render_report

    conn = report_store.connection
    conn.execute("INSERT INTO runs(id,status) VALUES ('previous','complete')")
    conn.execute(
        "INSERT INTO changes(id,run_id,repo,branch,status,reason,edits) VALUES "
        "('change-old','previous','example','dream/previous','waiting','checks failed','[]')"
    )
    conn.execute(
        "INSERT INTO changes(id,run_id,repo,status,edits) VALUES "
        "('change-new','dream-one','example','proposed',?)",
        (json.dumps([{"path": "Makefile", "before": "old", "after": "new"}]),),
    )
    conn.execute(
        "INSERT INTO sessions_seen(source,session_id,forgotten) VALUES ('claude','excluded-one',1)"
    )
    conn.execute(
        "INSERT INTO review_decisions VALUES "
        "('dream-one','candidate-one','{}','deferred','Too few independent sessions','item-one')"
    )
    page = render_report(report_store, "dream-one")
    assert "Too few independent sessions" in page
    assert "excluded-one" in page and "Forgotten sessions" in page
    assert "dream/previous" in page and "checks failed" in page
    assert "Makefile" in page and "Configuration, checks and restrictions" in page


def test_report_statistics_are_run_scoped_and_include_model_usage(report_store):
    from dreaming.report import render_report

    conn = report_store.connection
    conn.execute("INSERT INTO runs(id,status) VALUES ('other','complete')")
    conn.execute("INSERT INTO sessions_seen(source,session_id) VALUES ('claude','session-one')")
    for i in range(2):
        key = f"read-{i}"
        conn.execute(
            "INSERT INTO steps(run_id,key,phase,state) VALUES ('dream-one',?,'Read','done')", (key,)
        )
        conn.execute(
            "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,model,tokens_in,tokens_out,"
            "usage_estimated,seconds,outcome) VALUES (?,'dream-one',?,1,'reader','codex','model-x',"
            "10,5,1,1.5,'success')",
            (key, key),
        )
        conn.execute("INSERT INTO session_inputs VALUES (?,'claude','session-one')", (key,))
    conn.execute("INSERT INTO steps(run_id,key,phase) VALUES ('other','other-call','Read')")
    conn.execute(
        "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,tokens_in) "
        "VALUES ('other','other','other-call',1,'reader','unrelated',999999)"
    )
    page = render_report(report_store, "dream-one")
    assert "model-x" in page and "estimated" in page
    assert "Sessions split" in page and "Observations rejected" in page
    assert "unrelated" not in page and "999999" not in page


def test_report_extensions_render_trusted_internal_sections(report_store):
    from dreaming.report import render_report

    page = render_report(report_store, "dream-one", sections={"changes": "<p>Adoption results</p>"})
    assert "<p>Adoption results</p>" in page and "No repository changes." not in page


def test_report_uses_recorded_skips_and_tolerates_non_json_step_results(report_store):
    from dreaming.report import render_report

    report_store.connection.execute(
        "UPDATE runs SET statistics=?",
        (
            json.dumps(
                {
                    "sessions_skipped": 1,
                    "selection_skipped": [
                        {"source": "claude", "session_id": "recent-session", "reason": "recent"}
                    ],
                }
            ),
        ),
    )
    report_store.connection.execute(
        "INSERT INTO steps(run_id,key,phase,state,result,reason) "
        "VALUES ('dream-one','bad-output','Read','failed','unparsed response','schema_failure')"
    )
    page = render_report(report_store, "dream-one")
    assert "recent-session" in page and "schema_failure" in page
    assert "unparsed response" not in page


def test_report_refuses_a_reports_directory_inside_a_repository(report_store, tmp_path):
    from dreaming.report import write_report

    repo = tmp_path / "target"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (report_store.path.parent / "reports").symlink_to(repo, target_is_directory=True)
    with pytest.raises(ConfigError, match="outside every"):
        write_report(report_store, "dream-one")
    assert not (repo / "dream-one.html").exists()


def test_report_rejects_unknown_and_unsafe_run_ids(report_store):
    from dreaming.report import write_report

    with pytest.raises(ConfigError, match="Unknown run"):
        write_report(report_store, "unknown")
    with pytest.raises(ConfigError, match="Invalid run"):
        write_report(report_store, "../escape")


def test_vendored_report_assets_have_revision_and_actual_licence_status():
    from importlib.resources import files

    notice = files("dreaming.report").joinpath("UPSTREAM.txt").read_text()
    assert "5d07d2f7f31675709f57b35579f72058e846fa67" in notice
    assert "no licence" in notice.lower()
    assert "scripts/review_html" in notice


@pytest.mark.parametrize("applied", [False, True])
def test_adoption_report_lists_evidence_state_and_ordered_manual_commands(
    report_store, tmp_path, applied
):
    from dreaming.adopt import render_changes
    from dreaming.report import render_report

    repo = str(tmp_path / "repository with spaces")
    config = replace(
        load_config(tmp_path / "config.toml"),
        repositories={
            "target": {
                "path": repo,
                "generate": ["make generate"],
                "deploy": ["make deploy"],
            }
        },
    )
    report_store.connection.execute(
        "INSERT INTO changes(id,run_id,repo,branch,commit_sha,status,evidence,applied_at) "
        "VALUES ('change-one','dream-one',?,'dream/dream-one','change-sha',?,"
        "'[\"evidence-one\"]',?)",
        (repo, "applied" if applied else "waiting", "2026-11-15T00:00:00Z" if applied else None),
    )
    if applied:
        report_store.connection.execute(
            "INSERT INTO adoption(run_id,repo,branch,step,head_before,head_after,at) "
            "VALUES ('dream-one',?,'dream/dream-one','merged','base-sha','merge-sha','now')",
            (repo,),
        )
    page = render_report(
        report_store,
        "dream-one",
        sections={"changes": render_changes(report_store, "dream-one", config)},
    )
    assert "evidence-one" in page and ("applied" if applied else "waiting") in page
    scripts = [html.unescape(s) for s in re.findall(r"<pre>(.*?)</pre>", page, re.S)]
    expected = (
        ["revert --no-commit change-sha", "revert --no-commit -m 1 merge-sha"]
        if applied
        else ["merge --no-commit"]
    )
    for first in expected:
        script = next((s for s in scripts if first in s), None)
        assert script is not None, f"No manual command for {first}"
        assert script.index(first) < script.index("make generate")
        assert script.index("make generate") < script.index("commit --no-edit")
        assert script.index("commit --no-edit") < script.index("make deploy")


def test_report_labels_replay_results_as_association_with_counts(report_store):
    from dreaming.report import render_report

    conn = report_store.connection
    conn.execute(
        "INSERT INTO changes(id,run_id,repo,authoring_id) "
        "VALUES ('replayed','dream-one','target','author')"
    )
    conn.execute("INSERT INTO sessions_seen(source,session_id) VALUES ('claude','s')")
    conn.execute(
        "INSERT INTO replay_batches VALUES ('replayed','author','held','replay_recurrence')"
    )
    conn.execute(
        "INSERT INTO replays(change_id,authoring_id,source,session_id,backend,model,judgement,"
        "baseline_recurs,tokens) VALUES "
        "('replayed','author','claude','s','codex','model-x','unclear',1,25)"
    )
    page = render_report(report_store, "dream-one")
    assert "association, not proof" in page and "Recurring baselines" in page
    assert "Recurs with change" in page and "replay_recurrence" in page and "codex/model-x" in page


def test_reader_role_replays_do_not_count_as_sessions_read(report_store):
    from dreaming.report import render_report

    conn = report_store.connection
    conn.execute(
        "INSERT INTO sessions_seen(source,session_id) VALUES ('claude','replayed-session')"
    )
    conn.execute("INSERT INTO steps(run_id,key,phase) VALUES ('dream-one','lens-replay','Replay')")
    conn.execute(
        "INSERT INTO calls(id,run_id,task_key,attempt,role,backend,outcome) "
        "VALUES ('lens-call','dream-one','lens-replay',1,'reader','fake','success')"
    )
    conn.execute("INSERT INTO session_inputs VALUES ('lens-call','claude','replayed-session')")
    page = render_report(report_store, "dream-one")
    assert "<td>Sessions read</td><td>0</td>" in page
    assert "<td>Sessions split</td><td>0</td>" in page

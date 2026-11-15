"""The dream report (10.1–10.6, 7.4, 12.3, 15.2, 16.2).

Until adoption, effect measurement and the tooling inventory land, their
sections show an empty state; those tasks test their own sections.
"""

import json
import re
import webbrowser
from pathlib import Path

import pytest
from reportkit import (
    NOW,
    RUN,
    call,
    candidate,
    change,
    currency_finding,
    dream,
    finding,
    observation,
    page,
    pattern,
    read_step,
    recommendation,
    rejection,
    revision,
    section,
    source,
)
from runkit import (
    CallPhase,
    FakeClock,
    FakeModel,
    add_run,
    fake_config,
    make_runner,
    reads,
    run_row,
)

from dreaming import config, report
from dreaming.errors import DreamingError, Partial
from dreaming.report import sections, shell


@pytest.fixture(autouse=True)
def browser(monkeypatch):
    """The pages the report opened; no test opens a real browser."""
    opened = []

    def record(url, *args, **kwargs):
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    return opened


def cell(html, label):
    """The value next to `label` in a two-column table."""
    found = re.search(rf"<td>{re.escape(label)}</td><td>(.*?)</td>", html)
    return found.group(1) if found else None


# Writing and opening (10.1)


class Broken(CallPhase):
    def apply(self, conn, step, answers):
        raise KeyError("missing field")


@pytest.mark.parametrize(
    "ending", ["complete", "budget_exhausted", "partial", "interrupted"]
)
def test_every_ending_writes_the_report_outside_every_repository(db, home, ending):
    cfg = fake_config(tokens=100) if ending == "budget_exhausted" else fake_config()
    run_id = add_run(db, cfg=cfg)
    clock = FakeClock()

    def before(role, prompt):
        if ending == "partial" and role == "reviewer":
            raise Partial("no backend left for the reviewer")

    review = Broken("reviewer") if ending == "interrupted" else CallPhase("reviewer")
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader"), "review": review},
        model=FakeModel(clock, before=before),
        clock=clock,
        planner=reads(2),
        report=report.Writer(home, cfg),
    )
    assert runner.execute() == ending
    path = home / "reports" / f"{run_id}.html"
    assert path.is_file()
    assert run_row(db, run_id)["report_path"] == str(path)
    assert config.containing_repository(path.parent) is None
    html = path.read_text()
    label, tone, _ = sections.ENDINGS[ending]
    assert f'<span class="verdict-pill verdict-{tone}">{label}</span>' in html
    if ending in ("partial", "interrupted"):
        assert shell.escape(run_row(db, run_id)["error"]) in html


def test_the_report_opens_only_for_the_user_s_own_dreams(db, home, browser):
    cfg = fake_config()
    dream(db)
    dream(db, "dream-tool", requester="transit")
    path = report.Writer(home, cfg)(db, RUN)
    report.Writer(home, cfg)(db, "dream-tool")
    assert browser == [path.as_uri()]

    cfg["report"]["open_browser"] = False
    report.Writer(home, cfg)(db, RUN)
    assert browser == [path.as_uri()]


@pytest.mark.parametrize("failure", ["raises", "no browser"])
def test_a_browser_failure_never_fails_the_dream(
    db, home, monkeypatch, capsys, failure
):
    def broken(url, *args, **kwargs):
        if failure == "raises":
            raise webbrowser.Error("could not locate runnable browser")
        return False

    monkeypatch.setattr(webbrowser, "open", broken)
    cfg = fake_config()
    assert report.open_report(home / "page.html", "user", cfg) is False

    run_id = add_run(db, cfg=cfg)
    clock = FakeClock()
    runner = make_runner(
        db,
        home,
        run_id,
        cfg,
        phases={"read": CallPhase("reader")},
        model=FakeModel(clock),
        clock=clock,
        planner=reads(1, review=False),
        report=report.Writer(home, cfg),
    )
    assert runner.execute() == "complete"
    assert report.report_path(home, run_id).is_file()
    output = capsys.readouterr()
    assert "browser" in output.out
    assert "Traceback" not in output.err


def test_an_unknown_dream_has_no_report(db):
    with pytest.raises(DreamingError) as raised:
        page(db, "dream-missing")
    assert raised.value.code == "not_found"


# Layout (10.5, 12.3)


def test_the_page_uses_the_review_page_layout_and_style(db):
    dream(db)
    html = page(db)
    for marker in (
        '<header class="top-bar">',
        '<section class="card-grid">',
        '<nav class="toc">',
        "<footer>",
    ):
        assert marker in html
    assert shell.CSS in html
    toc = re.search(r'<nav class="toc">(.*?)</nav>', html, re.S).group(1)
    anchors = re.findall(r'<a href="#([^"]+)">', toc)
    assert anchors == re.findall(r'<section id="([^"]+)">', html)


def test_the_style_is_vendored_with_its_source_and_no_path_into_agentic_coding():
    assert "5d07d2f7f31675709f57b35579f72058e846fa67" in shell.__doc__
    assert "no licence" in shell.__doc__
    assert "--accent: #44C4DC;" in shell.CSS
    for module in Path(report.__file__).parent.glob("*.py"):
        text = module.read_text()
        assert "review_html import" not in text
        assert "import review_html" not in text
        assert "sys.path" not in text


# Statistics (10.3)


def test_statistics_count_sessions_observations_patterns_changes_and_use(db):
    dream(db)
    read_step(db, "claude", "a", parts=2)
    read_step(db, "claude", "b")
    read_step(db, "codex", "c", "skipped", "the session could not be parsed", parts=0)
    read_step(db, "claude", "d", "failed", "the reader failed twice", parts=0)
    call(
        db,
        "review",
        backend="codex",
        model="gpt-5",
        tokens=(1000, 200),
        seconds=30.0,
        estimated=1,
        role="reviewer",
    )
    pattern(db, "p1")
    observation(db, "o1", pattern_id="p1")
    observation(db, "o2", pattern_id="p1")
    observation(db, "o3")
    rejection(db, "observation", "the quote is not in the entry")
    rejection(db, "observation", "the quote is not in the entry")
    rejection(db, "knowledge", "the item changed since it was read")
    change(db, "ch1", "Add a rule")
    change(db, "ch2", "Name a customer", status="withheld")
    db.execute(
        "UPDATE runs SET active_seconds = 90, status = 'partial' WHERE id = ?", (RUN,)
    )

    stats = section(page(db), "statistics")
    assert cell(stats, "Sessions read") == "2 of 50"
    assert cell(stats, "Sessions split into parts") == "1"
    assert cell(stats, "Sessions skipped") == "1"
    assert cell(stats, "Sessions failed") == "1"
    assert cell(stats, "Observations accepted") == "3"
    assert cell(stats, "Observations rejected") == "2"
    assert cell(stats, "New patterns") == "1"
    assert cell(stats, "Patterns with new evidence") == "1"
    assert cell(stats, "Changes proposed / applied / waiting / withheld") == (
        "1 / 0 / 0 / 1"
    )
    assert cell(stats, "Tokens used") == "1,560 of 2,000,000, partly estimated"
    assert cell(stats, "Running time (minutes)") == "1.5 of 240"
    assert cell(stats, "Partial") == "yes"
    assert "codex:c</td><td>skipped</td><td>the session could not be parsed" in stats
    assert "claude:d</td><td>failed</td><td>the reader failed twice" in stats
    assert "<td>knowledge</td><td>the item changed since it was read</td>" in stats
    assert (
        "<td>codex</td><td>gpt-5</td><td>1</td><td>1,000</td><td>200</td><td>30.0</td>"
    ) in stats


# Knowledge, learning and changes kept apart (10.6, 15.2)


def test_retained_knowledge_learning_and_changes_are_kept_apart(db):
    dream(db)
    revision(db, "k-pref", "preference", "Prefers small commits")
    revision(db, "k-learn", "learning", "Asks how SwiftData migrations work")
    revision(
        db,
        "k-lint",
        "lesson",
        "Run lint before tests",
        status="contested",
        reason="two sessions disagree",
        contradicting=("o9",),
    )
    finding(
        db,
        "f1",
        "The guide covers lightweight migrations",
        [source("Migration guide", "https://example.com/guide")],
    )
    recommendation(db, "l1", "Learn SwiftData migrations", research=["f1"])
    change(db, "ch1", "Add a lint step to the skill")

    html = page(db)
    knowledge = section(html, "knowledge")
    learning = section(html, "learning")
    changes = section(html, "changes")
    assert knowledge.index("<h3>Preferences</h3>") < knowledge.index("<h3>Lessons</h3>")
    assert "<h3>Learning opportunities</h3>" in knowledge
    assert "Prefers small commits" in knowledge
    assert "contested" in knowledge and "two sessions disagree" in knowledge
    assert "1 supporting, 1 contradicting" in knowledge
    assert "projects /work/app" in knowledge

    assert "Learn SwiftData migrations" in learning
    assert "Learn SwiftData migrations" not in knowledge
    assert "Prefers small commits" not in learning
    observed = learning.index("Observed evidence")
    researched = learning.index("Researched, not observed")
    assert observed < researched
    assert "The guide covers lightweight migrations" in learning[researched:]
    assert "Add a lint step" not in knowledge + learning
    assert "Prefers small commits" not in changes

    assert "3 knowledge revisions retained" in html
    assert "1 learning recommendation<" in html
    assert '<a href="#changes">1 repository change</a>' in html


def test_a_dream_without_repository_changes_names_what_it_retained(db):
    dream(db)
    revision(db, "k1", "preference", "Prefers tabs")
    recommendation(db, "l1", "Learn regular expressions")
    statement = (
        "No repository changes. This dream&#x27;s result is the 1 knowledge "
        "revision and 1 learning recommendation it retained."
    )
    html = page(db)
    assert statement in section(html, "changes")
    assert statement in html.split('<nav class="toc">')[0]

    dream(db, "dream-empty")
    assert "no knowledge or learning recommendation was retained" in section(
        page(db, "dream-empty"), "changes"
    )


def test_effect_and_inventory_show_an_empty_state(db):
    dream(db)
    change(db, "ch1", "Add a rule")
    html = page(db)
    assert "not available yet" in section(html, "effect")
    assert "not measured yet" in section(html, "inventory")


def adoption(db, repo, *rows):
    for step, head_after in rows:
        db.execute(
            "INSERT INTO adoption (run, repo, branch, step, head_after, at)"
            " VALUES (?, ?, 'dream/dream-test', ?, ?, ?)",
            (RUN, repo, step, head_after, NOW),
        )


def test_changes_show_their_evidence_state_and_commands(db, tmp_path):
    repos = {name: tmp_path / name for name in ("agentic", "orbit", "notes", "skills")}
    cfg = config.build_config(
        {
            "repositories": {
                name: {
                    "path": str(path),
                    "generate": ["make generate"],
                    "deploy": ["make deploy"],
                }
                for name, path in repos.items()
            }
        }
    )
    dream(db)
    branch = "dream/dream-test"
    for change_id, repo, status, sha, reason in [
        ("ch1", "agentic", "applied", "a" * 40, None),
        ("ch2", "agentic", "applied", "b" * 40, None),
        ("ch3", "orbit", "waiting", "c" * 40, "auto-apply is disabled"),
        ("ch4", "notes", "withheld", None, "a credential or token in a.md"),
        (
            "ch5",
            "skills",
            "failed",
            "d" * 40,
            "the deploy command 'make deploy' failed",
        ),
    ]:
        change(db, change_id, f"Change {change_id}", repo=repo, status=status)
        db.execute(
            "UPDATE changes SET commit_sha = ?, reason = ?, evidence = ?,"
            " branch = ?, patch_path = ? WHERE id = ?",
            (
                sha,
                reason,
                json.dumps([f"e-{change_id}"]),
                branch if sha else None,
                None if sha else f"/home/.dreaming/patches/{change_id}.patch",
                change_id,
            ),
        )
    adoption(db, "agentic", ("merging", "m" * 40), ("merged", None), ("deployed", None))
    adoption(db, "skills", ("merging", "n" * 40), ("reverted", "r" * 40))

    changes = section(page(db, cfg=cfg), "changes")
    agentic, orbit = repos["agentic"], repos["orbit"]
    then = (
        "\ncd {path} &amp;&amp; make generate\ngit -C {path} add -A\n"
        "git -C {path} commit{message}\ncd {path} &amp;&amp; make deploy"
    )
    # Applied: a revert per change and one for the whole merge.
    assert (
        f"git -C {agentic} revert --no-commit {'a' * 40}"
        + then.format(path=agentic, message=" -m &#x27;Revert Change ch1&#x27;")
        in changes
    )
    assert f"git -C {agentic} revert --no-commit {'b' * 40}" in changes
    assert f"git -C {agentic} revert --no-commit -m 1 {'m' * 40}" in changes
    # Unapplied: a merge of the dream branch, with the reason it waits.
    assert (
        f"git -C {orbit} merge --no-commit {branch}"
        + then.format(path=orbit, message=" --no-edit")
        in changes
    )
    assert "auto-apply is disabled" in changes
    # A recovery revert is undone by reverting the revert.
    assert f"revert --no-commit {'r' * 40}" in changes
    # A withheld change names its patch and has no command.
    assert "/home/.dreaming/patches/ch4.patch" in changes
    assert f"git -C {repos['notes']}" not in changes
    for change_id in ("ch1", "ch2", "ch3", "ch4", "ch5"):
        assert f"<code>e-{change_id}</code>" in changes


def test_candidates_show_rank_signals_and_why_they_were_deferred_or_discarded(db):
    dream(db)
    candidate(
        db,
        "c2",
        "Rewrite the review skill",
        position=2,
        status="deferred",
        reason="the cited evidence was forgotten",
        deferred_steps=[
            {"skill": "pr-review", "step": "ask the user", "reason": "needs input"}
        ],
    )
    candidate(
        db,
        "c1",
        "Add a lint rule",
        position=1,
        status="authored",
        signals={"sessions": 4, "projects": 2, "recency": 0.8},
    )
    candidate(
        db,
        "c3",
        "Remove a rule",
        position=3,
        status="discarded",
        reason="the section is not in the file",
    )
    listed = section(page(db), "candidates")
    first = listed.index("Add a lint rule")
    assert first < listed.index("Rewrite the review skill")
    assert listed.index("Rewrite the review skill") < listed.index("Remove a rule")
    assert "4 sessions, 2 projects, recency 0.8" in listed
    assert "the cited evidence was forgotten" in listed
    assert "the section is not in the file" in listed
    assert "pr-review ask the user (needs input)" in listed


# Earlier branches, checks and forgotten sessions (10.4, 7.4)


def test_unmerged_branches_from_earlier_dreams_are_listed(db):
    earlier = "dream-earlier"
    dream(db, earlier, started_at="2026-11-01T09:00:00+00:00")
    branch = f"dream/{earlier}"
    change(db, "old1", "Proposed rule", branch=branch, run_id=earlier)
    change(db, "old2", "Waiting rule", branch=branch, status="waiting", run_id=earlier)
    change(db, "old3", "Applied rule", branch=branch, status="applied", run_id=earlier)
    change(db, "old4", "Withheld rule", status="withheld", run_id=earlier)
    dream(db)
    change(db, "new1", "This dream's rule", branch=f"dream/{RUN}")

    listed = section(page(db), "unmerged")
    assert f"<code>{branch}</code>" in listed and earlier in listed
    assert "Proposed rule (proposed)" in listed
    assert "Waiting rule (waiting)" in listed
    for left_out in ("Applied rule", "Withheld rule", "This dream"):
        assert left_out not in listed

    assert "No branch from an earlier dream" in section(page(db, earlier), "unmerged")


@pytest.mark.parametrize(
    "path, kind",
    [
        ("Makefile", "checks"),
        (".github/workflows/ci.yml", "checks"),
        (".claude/settings.json", "restrictions"),
        ("mcp/servers.json", "tool lists"),
        ("codex/config.toml", "tool lists"),
        ("instructions/lenses.toml", "dreaming's instructions"),
    ],
)
def test_changes_to_checks_restrictions_and_tool_lists_are_listed(db, path, kind):
    dream(db)
    change(db, "ch1", "Touch the setup", paths=(path, "docs/notes.md"))
    change(db, "ch2", "Only docs", paths=("docs/other.md",))
    listed = section(page(db), "config-changes")
    assert shell.escape(kind) in listed and f"<code>{path}</code>" in listed
    assert "Only docs" not in listed and "docs/notes.md" not in listed
    assert "applies from the next dream" in listed


def test_a_dream_without_config_changes_says_so(db):
    dream(db)
    change(db, "ch1", "Only docs", paths=("docs/other.md",))
    assert "No change in this dream touches checks" in section(
        page(db), "config-changes"
    )


def test_forgotten_sessions_are_listed(db):
    dream(db)
    assert section(page(db), "forgotten") is None
    db.execute(
        "INSERT INTO sessions_seen (source, session_id, state, forgotten)"
        " VALUES ('claude', 's-gone', 'read', 1), ('claude', 's-kept', 'read', 0)"
    )
    listed = section(page(db), "forgotten")
    assert "claude:s-gone" in listed and "s-kept" not in listed


# Currency review and fetched sources (15.2, 16.2, 16.4)


def test_currency_notes_and_changes_are_kept_apart_from_learning(db):
    dream(db)
    cited = [source("Current practice", "https://example.com/practice")]
    currency_finding(
        db, "cf1", "skills/review", "Reviews now cite sources", cited, status="note"
    )
    claim = "Property tests are common"
    currency_finding(db, "cf2", "rules/testing", claim, cited, status="proposed")
    recommendation(db, "l1", "Learn property testing")
    html = page(db)
    currency = section(html, "currency")
    assert currency.index("Reviews now cite sources") < currency.index(
        "Property tests are common"
    )
    assert ">note<" in currency and ">proposed<" in currency
    assert "Current practice" in currency
    assert "Reviews now cite sources" not in section(html, "learning")
    assert "Learn property testing" not in currency


def test_fetched_titles_are_text_and_only_http_links_are_rendered(db):
    dream(db)
    cited = [
        source("<script>alert(1)</script>", "https://example.com/a?x=1&y=2"),
        source("Plain http", "http://example.org/b"),
        source("Script link", "javascript:alert(1)"),
        source("Tabbed script", "java\tscript:alert(2)"),
        source("Data link", "data:text/html,<b>x</b>"),
        source("Local file", "rules/style.md@abc123"),
    ]
    db.execute(
        "INSERT INTO questions (id, run, question, unavailable, created_at)"
        " VALUES ('q1', ?, 'What is current?', ?, ?)",
        (
            RUN,
            json.dumps([{"source": "https://down.example/<x>", "reason": "timed out"}]),
            NOW,
        ),
    )
    finding(db, "f1", "A claim", cited, question_id="q1")
    currency_finding(db, "cf1", "skills/x", "A claim", cited, status="note")

    html = page(db)
    links = [h for h in re.findall(r'href="([^"]*)"', html) if not h.startswith("#")]
    assert set(links) == {"https://example.com/a?x=1&amp;y=2", "http://example.org/b"}
    assert "<script" not in html and "<b>x</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    for title in ("Script link", "Tabbed script", "Data link", "Local file"):
        assert title in html
    research = section(html, "research")
    assert "Could not be read, skipped." in research
    assert "https://down.example/&lt;x&gt;: timed out" in research


MARK = '<img src=x onerror="alert(1)">'


def test_every_stored_text_is_escaped(db):
    earlier = "dream-earlier"
    dream(db, earlier, started_at="2026-11-01T09:00:00+00:00")
    change(db, "old", MARK, repo=MARK, branch=MARK, run_id=earlier)
    dream(db, requester=MARK)
    db.execute(
        "UPDATE runs SET error = ?, revision = ? WHERE id = ?", (MARK, MARK, RUN)
    )
    read_step(db, "claude", MARK, "skipped", MARK, parts=0)
    call(db, "review", backend=MARK, model=MARK)
    rejection(db, MARK, MARK)
    revision(db, MARK, MARK, MARK, reason=MARK, scope={"projects": [MARK]})
    finding(db, "f1", MARK, [source(MARK, MARK, MARK)])
    recommendation(db, MARK, MARK, evidence=[MARK], research=["f1"])
    change(db, MARK, MARK, repo=MARK, paths=("Makefile", f"{MARK}/settings.json"))
    candidate(
        db,
        "c1",
        MARK,
        reason=MARK,
        deferred_steps=[{"skill": MARK, "step": MARK, "reason": MARK}],
    )
    db.execute(
        "INSERT INTO questions (id, run, question, unavailable, created_at)"
        " VALUES ('q1', ?, ?, ?, ?)",
        (RUN, MARK, json.dumps([{"source": MARK, "reason": MARK}]), NOW),
    )
    currency_finding(db, "cf1", MARK, MARK, [source(MARK, MARK)], status="note")
    db.execute(
        "INSERT INTO sessions_seen (source, session_id, state, forgotten)"
        " VALUES (?, ?, 'read', 1)",
        (MARK, MARK),
    )
    html = page(db)
    assert "<img" not in html
    assert html.count("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;") > 20

import json
import subprocess
from dataclasses import replace

import pytest
from test_authoring import commit, target  # noqa: F401
from test_dispatcher import FakeBackend
from test_reader import env as reader_fixture

from dreaming.backends import Dispatcher
from dreaming.currency import (
    CurrencyAuthorHandler,
    CurrencyResearchHandler,
    dismiss_finding,
    independent_sources,
    registrable_domain,
    schedule_currency,
)
from dreaming.knowledge import pattern_statistics
from dreaming.run import Engine
from dreaming.steps import CallResult

reader_env = reader_fixture

PAGES = {
    "https://a.example/one": ("Page A", "Intro. Use make targets for every build.  Outro."),
    "https://b.example.org/two": ("Page B", "Prefer\nmake targets for builds. More."),
    "https://www.a.example/three": ("Page A again", "Use make targets for every build."),
    "https://c.example/four": ("Page C", "Something else entirely."),
}


class Fetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in PAGES:
            raise OSError("not_found")
        return PAGES[url]


def source(url, passage, title="T"):
    return dict(title=title, url=url, retrieved_at="2026-11-17", claim="Use make", passage=passage)


A = source("https://a.example/one", "Use make targets for every build.")
B = source("https://b.example.org/two", "Prefer make targets for builds.")


def finding(*sources, text="Use make targets"):
    return dict(finding=text, sources=list(sources))


def researcher(*findings):
    return CallResult(json.dumps(dict(findings=list(findings), unavailable_sources=[])), model="m")


def authored(**changes):
    return json.dumps(
        dict(
            disposition="change",
            reason="Align with sources",
            evidence_ids=[],
            edits=[
                dict(
                    id="e",
                    path="rule.md",
                    before="Use direct commands.",
                    after="Use Make targets.",
                    supporting_evidence=[],
                    counter_evidence=[],
                )
            ],
            learning=None,
            external_input=True,
            depends_on=[],
            skill=None,
        )
        | changes
    )


def setup(env, target, **thresholds):  # noqa: F811
    store, config, roles, _ = env
    config = replace(
        config,
        repositories={"target": {"path": str(target), "default_branch": "main"}},
        thresholds=replace(config.thresholds, **thresholds),
        currency_targets=(dict(name="rules", repo=str(target), kind="rule", paths=("rule.md",)),),
    )
    roles.config = config
    store.connection.execute("UPDATE runs SET kind='currency' WHERE id='r'")
    schedule_currency(store, "r", config)
    return store, config, roles


DEFER = CallResult(authored(disposition="defer", edits=[]), model="m")


class NoAdopt:
    def apply(self, conn, step, raw):
        return ()


def run(store, config, roles, answers, fetch=None):
    fetch = fetch or Fetcher()
    research = CurrencyResearchHandler(
        store, "r", config, roles, fetch=fetch, now=lambda: "2026-11-17"
    )
    handlers = {
        "Research": research,
        "Author": CurrencyAuthorHandler(store, "r", config, roles),
        "Adopt": NoAdopt(),
    }
    backend = FakeBackend("fake", answers)
    code = Engine(
        store, "r", handlers, dispatcher=Dispatcher(store, "r", config, {"fake": backend})
    ).execute()
    return code, backend, fetch


def rows(store, sql):
    return store.connection.execute(sql).fetchall()


def test_helpers_count_domains_once_and_ignore_shared_passages():
    assert registrable_domain("https://www.a.example/x") == "a.example"
    assert registrable_domain("https://docs.b.co.uk/x") == "b.co.uk"
    sources = [A, source("https://www.a.example/3", "other"), B]
    assert [s["url"] for s in independent_sources(sources)] == [A["url"], B["url"]]
    shared = source("https://c.example/x", "make targets for every build")
    assert independent_sources([A, shared]) == [A]


def test_no_read_phase_no_sessions_and_findings_are_not_observations(reader_env, target):  # noqa: F811
    store, config, roles = setup(reader_env, target)
    before = rows(store, "SELECT count(*) FROM observations")
    code, backend, _ = run(
        store, config, roles, [researcher(finding(A, B)), CallResult(authored(), model="m")]
    )
    assert code == 0, rows(store, "SELECT notice FROM runs")
    assert {r[0] for r in rows(store, "SELECT DISTINCT phase FROM steps")} <= {
        "Research",
        "Author",
        "Adopt",
    }
    assert rows(store, "SELECT count(*) FROM session_inputs") == [(0,)]
    assert rows(store, "SELECT count(*) FROM sessions_seen") == [(0,)]
    assert rows(store, "SELECT count(*) FROM observations") == before
    assert rows(store, "SELECT kind FROM runs") == [("currency",)]
    assert (
        pattern_statistics(store, "p")["sessions"] == 0
        if "sessions" in pattern_statistics(store, "p")
        else True
    )
    assert len(backend.prompts) == 2


def test_one_source_is_only_a_note_and_same_domain_sources_count_once(reader_env, target):  # noqa: F811
    store, config, roles = setup(reader_env, target)
    same = source("https://www.a.example/three", "Use make targets for every build.")
    code, backend, _ = run(
        store, config, roles, [researcher(finding(A), finding(A, same, text="Second claim"))]
    )
    assert code == 0 and len(backend.prompts) == 1
    assert {r[0] for r in rows(store, "SELECT status FROM currency_findings")} == {"note"}
    assert rows(store, "SELECT count(*) FROM changes") == [(0,)]


def test_passage_missing_from_stored_text_rejects_the_finding(reader_env, target):  # noqa: F811
    store, config, roles = setup(reader_env, target)
    bad = source("https://b.example.org/two", "Text the page never said.")
    code, backend, _ = run(store, config, roles, [researcher(finding(A, bad))])
    assert code == 0 and len(backend.prompts) == 1
    assert rows(store, "SELECT count(*) FROM currency_findings") == [(0,)]
    reasons = rows(store, "SELECT disposition,reason FROM review_decisions")
    assert ("rejected", "passage_not_in_stored_text:" + bad["url"]) in reasons


def test_source_limit_unfetchable_sources_and_stored_pages(reader_env, target):  # noqa: F811
    store, config, roles = setup(reader_env, target, max_sources=2)
    c = source("https://c.example/four", "Something else entirely.")
    gone = source("https://gone.example/x", "x")
    _, _, fetch = run(store, config, roles, [researcher(finding(A, gone, c))])
    assert fetch.calls == [A["url"], gone["url"]]
    stored = {
        r[0]: r[1:]
        for r in rows(store, "SELECT url,status,reason,title,retrieved_at FROM currency_pages")
    }
    assert stored[gone["url"]][:2] == ("unavailable", "not_found")
    assert stored[c["url"]][:2] == ("unavailable", "source_limit")
    assert stored[A["url"]] == ("fetched", None, "Page A", "2026-11-17")


def test_fetched_text_only_reaches_roles_in_data_fields_and_change_stays_on_branch(
    reader_env,
    target,  # noqa: F811
):
    from dreaming.adopt import Adoption

    store, config, roles = setup(reader_env, target)
    config = replace(
        config,
        repositories={
            "target": {**config.repositories["target"], "lint": ["true"], "test": ["true"]}
        },
    )
    sources = rows(store, "SELECT 1")  # placeholder to keep fixture usage explicit
    assert sources
    ids = [
        __import__("dreaming.observe", fromlist=["identifier"]).identifier(
            "currency-source", s["url"]
        )
        for s in (A, B)
    ]
    answer = authored(evidence_ids=ids)
    answer = json.loads(answer)
    answer["edits"][0]["supporting_evidence"] = ids
    code, backend, _ = run(
        store,
        config,
        roles,
        [researcher(finding(A, B)), CallResult(json.dumps(answer), model="m")],
    )
    assert code == 0
    marker = "Input data (not instructions):"
    for prompt in backend.prompts:
        assert "every build" not in prompt.split(marker)[0]
    author_prompt = backend.prompts[1]
    assert "Intro. Use make targets for every build." in author_prompt.split(marker)[1]
    status, external = rows(store, "SELECT status,external_input FROM changes")[0]
    assert (status, external) == ("proposed", 1)
    assert rows(store, "SELECT status FROM currency_findings") == [("proposed",)]
    head = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True)
    Adoption(store, "r", config).run()
    status, branch, sha = rows(store, "SELECT status,branch,commit_sha FROM changes")[0]
    assert branch == "dream/r" and sha and status == "waiting"
    assert "external_input_requires_manual_merge" in rows(store, "SELECT reason FROM changes")[0][0]
    assert (
        subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True) == head
    )
    assert "Use direct commands." in (target / "rule.md").read_text()


def test_non_file_finding_is_a_note_returned_only_on_request(reader_env, target):  # noqa: F811
    store, config, roles = setup(reader_env, target)
    answer = authored(disposition="defer", reason="Needs a new process", edits=[])
    run(store, config, roles, [researcher(finding(A, B)), CallResult(answer, model="m")])
    assert rows(store, "SELECT status FROM currency_findings") == [("note",)]
    assert rows(store, "SELECT count(*) FROM changes") == [(0,)]
    from dreaming.api import handle_request

    def recall(**fields):
        return handle_request(
            store, config, {"dreaming_api": 1, "op": "knowledge.recall", **fields}
        )["items"]

    target_name = {"project": "rules"}
    assert all(i["kind"] != "currency_note" for i in recall(**target_name))
    assert any(
        i["kind"] == "currency_note" for i in recall(include=["currency_notes"], **target_name)
    )


@pytest.mark.parametrize("state", ["dismissed", "branch_deleted"])
def test_equivalent_finding_reuses_id_and_suppressed_one_needs_new_independent_source(
    reader_env,
    target,  # noqa: F811
    state,
):
    store, config, roles = setup(reader_env, target)
    run(store, config, roles, [researcher(finding(A, B)), DEFER])
    (fid,) = [r[0] for r in rows(store, "SELECT id FROM currency_findings")]
    if state == "dismissed":
        dismiss_finding(store, fid)
    else:
        store.connection.execute("UPDATE currency_findings SET status='branch_deleted'")
    # Same claim, same domains: equivalent, reuses the id, stays suppressed.
    for table in ("calls", "review_decisions", "steps"):
        store.connection.execute(f"DELETE FROM {table}")
    schedule_currency(store, "r", config)
    store.connection.execute("DELETE FROM currency_pages")
    run(store, config, roles, [researcher(finding(A, B, text="use  make   targets"))])
    assert [r[0] for r in rows(store, "SELECT id FROM currency_findings")] == [fid]
    assert rows(store, "SELECT status FROM currency_findings") == [(state,)]
    assert ("deferred", "no_new_independent_source") in rows(
        store, "SELECT disposition,reason FROM review_decisions"
    )
    # A new independent source reopens it under the same identifier.
    c = source("https://c.example/four", "Something else entirely.")
    PAGES["https://c.example/four"] = (
        "Page C",
        "Use make targets for every build, says C. Something else entirely.",
    )
    c["passage"] = "Use make targets for every build, says C."
    for table in ("calls", "review_decisions", "steps"):
        store.connection.execute(f"DELETE FROM {table}")
    store.connection.execute("DELETE FROM currency_pages")
    schedule_currency(store, "r", config)
    run(
        store,
        config,
        roles,
        [researcher(finding(A, c)), CallResult(authored(disposition="defer", edits=[]), model="m")],
    )
    assert [r[0] for r in rows(store, "SELECT id FROM currency_findings")] == [fid]
    assert rows(store, "SELECT status FROM currency_findings") == [("note",)]
    PAGES["https://c.example/four"] = ("Page C", "Something else entirely.")


def test_discarded_currency_branch_stays_suppressed(reader_env, target):  # noqa: F811
    from dreaming.adopt import discard

    store, config, roles = setup(reader_env, target)
    store.connection.execute("UPDATE runs SET snapshot_path=NULL")
    run(store, config, roles, [researcher(finding(A, B)), DEFER])
    store.connection.execute("UPDATE currency_findings SET status='proposed'")
    discard(store, dict(id="r", process_group=None), config=config)
    assert rows(store, "SELECT status FROM currency_findings") == [("branch_deleted",)]

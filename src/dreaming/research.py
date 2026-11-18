"""The Research step: one researcher call per question (4.2, 4.6, 13.6, 15.2).

The researcher may read the configured repositories and knowledge folders and
use whatever web and pull-request tools its role lists. A missing folder or
repository is reported and skipped; research goes on without it.

Findings are stored apart from observations, each with its sources, and never
count as evidence about the user. A finding is `fetched` unless every source it
cites is a local path in a repository or knowledge folder. The link decides as
well as the kind the model gave, so fetched text labelled as local still counts
as fetched (9.19).
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dreaming import knowledge, roles, steps
from dreaming.errors import Rejected
from dreaming.observe import evidence_input_ref
from dreaming.run import Context, Outcome
from dreaming.steps import Step

LOCAL_KINDS = ("repository", "knowledge_folder")
# A cited source as it is shown outside the step that found it.
CITED = ("title", "link", "retrieved_on", "passage")


def fetched(source: Mapping) -> bool:
    """Whether a source's text came from the web or a pull request: anything
    but a local repository or knowledge-folder path."""
    link = source["link"].strip().lower()
    url = "://" in link or link.startswith("www.")
    return source["kind"] not in LOCAL_KINDS or url


def readable(cfg: dict) -> tuple[list[dict], list[str], list[dict]]:
    """The repositories and knowledge folders the researcher may read, and the
    missing ones, which are reported and skipped (4.6)."""
    repositories, missing = [], []
    for name, repo in cfg["repositories"].items():
        path = Path(repo["path"]).expanduser()
        if path.is_dir():
            repositories.append(
                {
                    "name": name,
                    "path": str(path),
                    "default_branch": repo["default_branch"],
                }
            )
        else:
            missing.append(
                {"source": str(path), "reason": "the repository does not exist"}
            )
    folders = []
    for raw in cfg["knowledge_folders"]:
        path = Path(raw).expanduser()
        if path.is_dir():
            folders.append(str(path))
        else:
            missing.append(
                {"source": str(path), "reason": "the knowledge folder does not exist"}
            )
    return repositories, folders, missing


def check_finding(found: Mapping) -> None:
    """A finding must state a claim and cite sources, each with a link, the
    passage relied on and the date it was read."""
    if not found["claim"].strip():
        raise Rejected("the finding states no claim")
    if not found["sources"]:
        raise Rejected("the finding cites no source")
    for source in found["sources"]:
        if not source["link"].strip() or not source["passage"].strip():
            raise Rejected(f"source {source['title']!r} has no link or passage")
        try:
            date.fromisoformat(source["retrieved_on"])
        except ValueError:
            raise Rejected(
                f"source {source['title']!r} has no retrieval date"
            ) from None


@dataclass
class Researched:
    question_id: str
    question: str
    now: str
    unavailable: list[dict] = field(default_factory=list)
    output: dict | None = None
    backend: str | None = None
    model: str | None = None


class ResearchPhase:
    def __init__(self, instructions: roles.InstructionSet) -> None:
        self.instructions = instructions

    def prepare(self, ctx: Context, step: Step) -> Researched:
        conn = ctx.conn
        row = conn.execute(
            "SELECT * FROM questions WHERE id = ?", (step.input_ref,)
        ).fetchone()
        repositories, folders, missing = readable(ctx.cfg)
        data = {
            "question": row["question"],
            "candidate": None,
            "repositories": repositories,
            "knowledge_folders": folders,
        }
        evidence: list[str] = []
        if row["candidate_id"]:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (row["candidate_id"],)
            ).fetchone()
            data["candidate"] = {
                "id": candidate["id"],
                "proposal": candidate["proposal"],
                "summary": candidate["summary"],
                "repo": candidate["repo"],
            }
            evidence = json.loads(candidate["evidence"])
        role = self.instructions.roles["researcher"]
        result = Researched(row["id"], row["question"], ctx.runner.now(), missing)
        result.output = ctx.call(
            "researcher",
            role.prompt(data),
            role.schema,
            input_ref=evidence_input_ref(conn, evidence),
        )
        result.backend, result.model = steps.answered_by(conn, ctx.run_id, step.key)
        return result

    def apply(self, conn: sqlite3.Connection, step: Step, r: Researched) -> Outcome:
        for index, found in enumerate(r.output["findings"]):
            try:
                check_finding(found)
            except Rejected as rejected:
                steps.record_rejection(
                    conn,
                    step.run,
                    step.key,
                    "finding",
                    r.question_id,
                    rejected.reason,
                    {"claim": found["claim"][:200]},
                )
                continue
            conn.execute(
                "INSERT OR IGNORE INTO research_findings (id, run, question,"
                " question_id, finding, assessment, sources, fetched, backend,"
                " model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    knowledge.new_id("rf", step.run, r.question_id, index),
                    step.run,
                    r.question,
                    r.question_id,
                    found["claim"].strip(),
                    found["assessment"],
                    json.dumps(found["sources"]),
                    int(any(fetched(source) for source in found["sources"])),
                    r.backend,
                    r.model,
                    r.now,
                ),
            )
        conn.execute(
            "UPDATE questions SET unavailable = ? WHERE id = ?",
            (json.dumps([*r.unavailable, *r.output["unavailable"]]), r.question_id),
        )
        return Outcome()

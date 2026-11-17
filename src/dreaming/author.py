"""The Author step: one candidate to a proposed repository change or a learning
recommendation (4.2, 4.4, 9.2, 9.3, 9.10, 9.19, 11.5, 15.1, 15.2).

Before any call the program checks that the candidate's evidence still counts
and still meets its threshold, and that the files it cites still exist on the
repository's default branch; otherwise the candidate is deferred with the
reason (11.5). The author then checks the evidence against the current files
itself and may defer too.

The author sees the default branch, never the user's working tree: files and
the repository's own instruction files (`CLAUDE.md`, `AGENTS.md`) are read with
git and sent in the prompt, so they count toward the budget and no harness
loads them itself (4.4). Its answer is checked before anything is stored. A
change is a set of edits to named sections of the current files, never a
whole-file rewrite, each citing a supporting case and any counter case from
the evidence it was given. A learning recommendation keeps its research apart
from its evidence. The program, not the model, writes the evidence trailer of
the commit message (9.2).

A change is written by the author itself, with its file tools, in the run's
work copy of the repository on the dream branch, the only directory it may
write (9.1, 9.4). Once the answer passes its checks, the program commits the
edits as the change's one commit (`adopt.commit_change`) before the step's
results are stored, so the next candidate's call starts from a clean copy. The
change is then `proposed`, or `withheld` with its patch when the public scan or
a generate command stops it (9.5, 9.12).
"""

import json
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from dreaming import adopt, config, knowledge, research, roles, scan, steps
from dreaming.errors import Rejected
from dreaming.observe import evidence_input_ref, verify
from dreaming.run import Context, Outcome
from dreaming.steps import Step

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
TRAILER = "Dreaming-Evidence"


# The default branch, read with git


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


@dataclass(frozen=True)
class Repository:
    name: str
    path: Path
    default_branch: str
    revision: str
    public: bool

    def read(self, path: str) -> str | None:
        """A file's text at the revision, or None when there is no such file."""
        result = _git(self.path, "cat-file", "blob", f"{self.revision}:{path}")
        return result.stdout if result.returncode == 0 else None

    def exists(self, path: str) -> bool:
        """Whether a file or directory is at `path` in the revision."""
        return (
            _git(self.path, "cat-file", "-e", f"{self.revision}:{path}").returncode == 0
        )

    def instruction_files(self) -> dict[str, str]:
        """The repository's own instruction files, which describe its
        conventions (4.4)."""
        found = {}
        for name in INSTRUCTION_FILES:
            text = self.read(name)
            if text is not None:
                found[name] = text
        return found

    def view(self) -> dict:
        return {
            "name": self.name,
            "default_branch": self.default_branch,
            "revision": self.revision,
            "public": self.public,
        }


def repository(cfg: dict, name: str) -> Repository:
    """A configured repository at its default branch's current commit."""
    entry = cfg["repositories"].get(name)
    if entry is None:
        raise Rejected(f"{name} is not a configured repository")
    path = Path(entry["path"]).expanduser()
    branch = entry["default_branch"]
    result = _git(path, "rev-parse", "--verify", f"{branch}^{{commit}}")
    if result.returncode != 0:
        raise Rejected(
            f"repository {name} is unavailable: {result.stderr.strip() or 'no git'}"
        )
    return Repository(name, path, branch, result.stdout.strip(), entry["public"])


def inside(path: str) -> bool:
    """Whether an edit path names a file inside the repository, outside `.git`."""
    parts = PurePosixPath(path).parts
    return (
        bool(parts)
        and not path.startswith("/")
        and "\\" not in path
        and ".." not in parts
        and parts[0] != ".git"
    )


# What the author is given


def _counter(conn: sqlite3.Connection, pattern_id: str | None, evidence) -> list:
    """Counted evidence contradicting knowledge that rests on the pattern: the
    cases where a change must not apply."""
    rows = conn.execute(
        "SELECT r.contradicting FROM knowledge_items k JOIN knowledge_revisions r"
        " ON r.item_id = k.id AND r.version = k.version WHERE k.pattern_id = ?"
        " AND k.status NOT IN ('superseded', 'dismissed')",
        (pattern_id,),
    )
    ids = {i for row in rows for i in json.loads(row[0])} - set(evidence)
    return sorted(knowledge.counted_ids(conn, ids))


def _research(conn: sqlite3.Connection, candidate_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT f.* FROM research_findings f JOIN questions q ON q.id = f.question_id"
        " WHERE q.candidate_id = ? ORDER BY f.id",
        (candidate_id,),
    ).fetchall()


def _fit(cfg: dict, role: roles.Role, data: dict) -> str:
    """The prompt, leaving out file texts, largest first, until it fits the
    role's smallest context; the author can read what was left out (5.2)."""
    files = data["files"]
    while True:
        prompt = role.prompt(data)
        if roles.fits(cfg, "author", prompt):
            return prompt
        if not files:
            raise Rejected("the candidate's input does not fit the author's context")
        largest = max(files, key=lambda path: (len(files[path]), path))
        del files[largest]
        data["omitted_files"] = sorted([*data["omitted_files"], largest])


@dataclass
class Given:
    """What the author was given, which its answer may cite."""

    evidence: list[str]
    counter: list[str]
    research: list[str]
    fetched: bool
    repo: Repository | None
    # The currency finding a currency candidate rests on (16.3).
    currency: str | None = None


@dataclass
class Authored:
    cfg: dict
    candidate: dict
    now: str
    deferred: str | None = None
    given: Given | None = None
    output: dict | None = None
    backend: str | None = None
    model: str | None = None
    # Each edited path's text on the default branch, None for a new file.
    current: dict[str, str | None] = field(default_factory=dict)
    # A change answer that passed its checks, and its commit or withholding.
    change: dict | None = None
    committed: adopt.Committed | None = None
    rejected: str | None = None


class AuthorPhase:
    def __init__(self, instructions: roles.InstructionSet) -> None:
        self.instructions = instructions

    def prepare(self, ctx: Context, step: Step) -> Authored:
        conn, cfg = ctx.conn, ctx.cfg
        row = conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (step.input_ref,)
        ).fetchone()
        authored = Authored(cfg, dict(row), ctx.runner.now())
        role = self.instructions.roles["author"]
        try:
            given, data = self._input(conn, cfg, authored.candidate)
            if given.repo is not None:
                work_path = adopt.work_path(ctx.home, ctx.run_id, given.repo.name)
                data["repository"]["work_copy"] = str(work_path)
            prompt = _fit(cfg, role, data)
        except Rejected as rejected:
            authored.deferred = rejected.reason
            return authored
        authored.given = given
        work = None
        if given.repo is not None:
            work = adopt.work_copy(ctx.home, cfg, ctx.run_id, given.repo.name)
        authored.output = ctx.call(
            "author",
            prompt,
            role.schema,
            input_ref=evidence_input_ref(conn, [*given.evidence, *given.counter]),
            work=work,
        )
        authored.backend, authored.model = steps.answered_by(conn, ctx.run_id, step.key)
        if work is None:
            return authored
        paths = {
            edit["path"]
            for change in authored.output["changes"]
            for edit in change["edits"]
            if inside(edit["path"])
        }
        authored.current = {path: given.repo.read(path) for path in sorted(paths)}
        out = authored.output
        if out["outcome"] == "change" and out["verification"]["supported"]:
            try:
                authored.change = _check_change(conn, cfg, ctx.run_id, authored)
                authored.committed = adopt.commit_change(
                    ctx.home,
                    cfg,
                    ctx.run_id,
                    work,
                    authored.change["id"],
                    authored.change["commit_message"],
                    {edit["path"] for edit in authored.change["edits"]},
                    scan.evidence_quotes(conn, [*given.evidence, *given.counter]),
                )
            except Rejected as rejected:
                authored.rejected = rejected.reason
        return authored

    def _input(self, conn: sqlite3.Connection, cfg: dict, c: Mapping):
        """The author's input, after checking that the candidate's evidence
        still holds against the current files (11.5)."""
        if c["currency_finding_id"]:
            return self._currency_input(conn, cfg, c)
        evidence = json.loads(c["evidence"])
        knowledge.check_evidence(conn, evidence)
        kind = "learning" if c["proposal"] == "learning" else ""
        support = knowledge.item_support(conn, kind, evidence)
        if not knowledge.support_holds(support, cfg):
            raise Rejected(
                f"its evidence now spans {support.sessions} of the "
                f"{cfg['thresholds']['min_sessions']} distinct sessions needed (4.5)"
            )
        repo = repository(cfg, c["repo"]) if c["proposal"] == "change" else None
        if repo is not None:
            _check_unfinished(conn, cfg, repo, c["run"])
        cited = conn.execute(
            "SELECT repo, path FROM file_findings"
            " WHERE evidence_id IN (SELECT value FROM json_each(?)) ORDER BY path",
            (json.dumps(evidence),),
        ).fetchall()
        files: dict[str, str] = {}
        for found in cited:
            if found["repo"] not in cfg["repositories"]:
                continue
            ours = repo is not None and found["repo"] == repo.name
            text = (repo if ours else repository(cfg, found["repo"])).read(
                found["path"]
            )
            if text is None:
                raise Rejected(f"{found['path']} no longer exists in {found['repo']}")
            if ours:
                files[found["path"]] = text
        quote_chars = cfg["thresholds"]["quote_chars"]
        counter = _counter(conn, c["pattern_id"], evidence)
        research = _research(conn, c["id"])
        data = {
            "candidate": {
                "id": c["id"],
                "proposal": c["proposal"],
                "summary": c["summary"],
                "reason": c["reason"],
                "repo": c["repo"],
            },
            "evidence": knowledge.evidence_refs(
                conn, evidence, quote_chars, counted=True
            ),
            "counter_evidence": knowledge.evidence_refs(
                conn, counter, quote_chars, counted=True
            ),
            "research": [
                {
                    "id": row["id"],
                    "question": row["question"],
                    "claim": row["finding"],
                    "assessment": row["assessment"],
                    "sources": json.loads(row["sources"]),
                }
                for row in research
            ],
            "repository": repo.view() if repo else None,
            "instruction_files": repo.instruction_files() if repo else {},
            "files": files,
            "omitted_files": [],
        }
        given = Given(
            evidence=sorted(evidence),
            counter=counter,
            research=[row["id"] for row in research],
            fetched=any(row["fetched"] for row in research),
            repo=repo,
        )
        return given, data

    def _currency_input(self, conn: sqlite3.Connection, cfg: dict, c: Mapping):
        """The input for a currency candidate: its finding with the passages it
        relies on, as data, and the target's current text (16.3). The finding is
        the change's only evidence; it is not an observation (16.5)."""
        found = conn.execute(
            "SELECT * FROM currency_findings WHERE id = ?", (c["currency_finding_id"],)
        ).fetchone()
        repo = repository(cfg, c["repo"])
        _check_unfinished(conn, cfg, repo, c["run"])
        _, path = config.split_target(found["target"])
        text = repo.read(path)
        data = {
            "candidate": {
                "id": c["id"],
                "proposal": c["proposal"],
                "summary": c["summary"],
                "reason": c["reason"],
                "repo": c["repo"],
            },
            "evidence": [
                {
                    "id": found["id"],
                    "type": "currency_finding",
                    "target": found["target"],
                    "claim": found["claim"],
                    "assessment": found["assessment"],
                    # The passages relied on, never the stored page (16.3).
                    "sources": [
                        {key: s[key] for key in research.CITED}
                        for s in json.loads(found["sources"])
                    ],
                }
            ],
            "counter_evidence": [],
            "research": [],
            "repository": repo.view(),
            "instruction_files": repo.instruction_files(),
            "files": {} if text is None else {path: text},
            "omitted_files": [],
        }
        given = Given(
            evidence=[found["id"]],
            counter=[],
            research=[],
            fetched=True,
            repo=repo,
            currency=found["id"],
        )
        return given, data

    def apply(self, conn: sqlite3.Connection, step: Step, a: Authored) -> Outcome:
        c = a.candidate
        if a.deferred is not None:
            return _defer(conn, c, a.deferred, a.now)
        out = a.output
        conn.execute(
            "UPDATE candidates SET deferred_steps = ? WHERE id = ?",
            (json.dumps(out["deferred_steps"]), c["id"]),
        )
        if out["outcome"] == "defer" or not out["verification"]["supported"]:
            reason = out["verification"]["reason"].strip() or "the author deferred it"
            return _defer(conn, c, reason, a.now)
        cfg = a.cfg
        try:
            if out["outcome"] != c["proposal"]:
                raise Rejected(
                    f"a {c['proposal']} candidate was authored as {out['outcome']}"
                )
            if out["outcome"] == "learning":
                made = _store_learning(conn, cfg, step.run, a)
            elif a.rejected is not None:
                raise Rejected(a.rejected)
            else:
                made = _store_change(conn, step.run, a)
        except Rejected as rejected:
            steps.record_rejection(
                conn,
                step.run,
                step.key,
                "author",
                c["id"],
                rejected.reason,
                {"outcome": out["outcome"]},
            )
            conn.execute(
                "UPDATE candidates SET status = 'discarded', reason = ? WHERE id = ?",
                (rejected.reason, c["id"]),
            )
            _currency_note(conn, c, a.now, rejected.reason)
            return Outcome(reason=f"discarded: {rejected.reason}")
        conn.execute(
            "UPDATE candidates SET status = 'authored' WHERE id = ?", (c["id"],)
        )
        if c["currency_finding_id"]:
            conn.execute(
                "UPDATE currency_findings SET status = 'proposed', reason = NULL,"
                " updated_at = ? WHERE id = ?",
                (a.now, c["currency_finding_id"]),
            )
        return Outcome(reason=f"authored {made}")


def _check_unfinished(
    conn: sqlite3.Connection, cfg: dict, repo: Repository, run_id: str
) -> None:
    earlier = adopt.unfinished_adoption(conn, cfg, repo.name, run_id)
    if earlier:
        raise Rejected(
            f"dream {earlier['run']} merged into {repo.name} without finishing "
            "its deploy; finish or undo that adoption first"
        )


def _currency_note(conn: sqlite3.Connection, c: Mapping, now: str, reason: str):
    """A currency candidate that was not authored as a change is kept as a
    currency note (16.4)."""
    if c["currency_finding_id"]:
        conn.execute(
            "UPDATE currency_findings SET status = 'note', reason = ?, updated_at = ?"
            " WHERE id = ?",
            (f"not authored: {reason}", now, c["currency_finding_id"]),
        )


def _defer(conn: sqlite3.Connection, c: Mapping, reason: str, now: str) -> Outcome:
    conn.execute(
        "UPDATE candidates SET status = 'deferred', reason = ? WHERE id = ?",
        (reason, c["id"]),
    )
    _currency_note(conn, c, now, reason)
    return Outcome(reason=f"deferred: {reason}")


# Checking and storing the answer


def _given(ids: Iterable[str], allowed: Iterable[str], what: str) -> None:
    missing = sorted(set(ids) - set(allowed))
    if missing:
        raise Rejected(f"{what} cites ids that were not given: {', '.join(missing)}")


def _check_edit(edit: Mapping, current: Mapping, given: Given) -> None:
    path, action = edit["path"], edit["action"]
    if not inside(path):
        raise Rejected(f"edit path {path!r} is not a path inside the repository")
    section = edit["section"].strip()
    if not section:
        raise Rejected(f"an edit to {path} names no section")
    if section.lower() in (path.lower(), PurePosixPath(path).name.lower()):
        raise Rejected(f"an edit to {path} names the whole file, not a section")
    text = current.get(path)
    if text is None:
        if action != "add":
            raise Rejected(f"{path} does not exist, so it can only be added")
    elif not verify(section, text):
        raise Rejected(f"section {section!r} is not in {path}")
    if action == "delete":
        if edit["text"] is not None:
            raise Rejected(f"a delete takes no text ({path})")
    elif not (edit["text"] or "").strip():
        raise Rejected(f"the {action} in {path} needs text")
    if not edit["supporting"]:
        raise Rejected(f"the edit to {path} at {section!r} cites no supporting case")
    _given(edit["supporting"], given.evidence, f"the edit to {path}")
    _given(edit["counter"], [*given.evidence, *given.counter], f"the edit to {path}")


def commit_message(
    conn: sqlite3.Connection, message: str, evidence: Sequence[str], public: bool
) -> str:
    """The author's subject and body, then one trailer per cited evidence id. A
    file finding also names its repository, revision, path and location, except
    in a public repository, which cites evidence by id only (9.2, 9.12)."""
    found = {
        row["evidence_id"]: row
        for row in conn.execute(
            "SELECT * FROM file_findings"
            " WHERE evidence_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(evidence)),),
        )
    }
    trailers = []
    for evidence_id in evidence:
        row = found.get(evidence_id)
        cited = evidence_id
        if row is not None and not public:
            cited += (
                f" ({row['repo']} {row['revision']} {row['path']} {row['location']})"
            )
        trailers.append(f"{TRAILER}: {cited}")
    return f"{message.strip()}\n\n" + "\n".join(trailers)


def _check_change(conn: sqlite3.Connection, cfg: dict, run_id: str, a: Authored):
    """The change an answer proposes, checked against the authoring rules; it is
    `Rejected` with the first rule it breaks."""
    c, out, given = a.candidate, a.output, a.given
    if out["learning"] or len(out["changes"]) != 1:
        raise Rejected(
            "a change answer holds exactly one change and no learning recommendation"
        )
    change = out["changes"][0]
    if change["repo"] != c["repo"]:
        raise Rejected(f"the change is for {change['repo']}, not {c['repo']}")
    if not change["evidence"]:
        raise Rejected("the change cites no evidence")
    lines = change["commit_message"].splitlines()
    if not lines or not lines[0].strip():
        raise Rejected("the commit message has no subject")
    depends_on = sorted(set(change["depends_on"]))
    for other in depends_on:
        if other == c["repo"]:
            raise Rejected("the change depends on its own repository")
        if other not in cfg["repositories"]:
            raise Rejected(
                f"the change depends on {other}, which is not a configured repository"
            )
    if not change["edits"]:
        raise Rejected("the change makes no edit")
    for edit in change["edits"]:
        _check_edit(edit, a.current, given)
    supporting = {i for edit in change["edits"] for i in edit["supporting"]}
    evidence = sorted(set(change["evidence"]) | supporting)
    _given(evidence, given.evidence, "the change")
    counter = {i for edit in change["edits"] for i in edit["counter"]}
    if given.currency is None:
        knowledge.check_evidence(conn, [*evidence, *counter])
    external = given.fetched or any(
        tool not in config.BUILTIN_TOOLS for tool in cfg["tools"]["author"]
    )
    return {
        "id": knowledge.new_id("ch", run_id, c["id"]),
        "summary": change["summary"],
        "commit_message": commit_message(
            conn, change["commit_message"], evidence, given.repo.public
        ),
        "depends_on": depends_on,
        "evidence": evidence,
        "edits": change["edits"],
        "external_input": external,
    }


def _store_change(conn: sqlite3.Connection, run_id: str, a: Authored) -> str:
    """The checked change, `proposed` with its commit on the dream branch or
    `withheld` with the reason and its saved patch."""
    c, change, committed = a.candidate, a.change, a.committed
    withheld = committed.withheld is not None
    conn.execute(
        "INSERT INTO changes (id, run, pattern_id, candidate_id, currency_finding_id,"
        " repo, summary, commit_message, depends_on, evidence, edits, research,"
        " external_input, status, reason, branch, commit_sha, patch_path, rank,"
        " base_revision, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            change["id"],
            run_id,
            c["pattern_id"],
            c["id"],
            a.given.currency,
            c["repo"],
            change["summary"],
            change["commit_message"],
            json.dumps(change["depends_on"]),
            json.dumps(change["evidence"]),
            json.dumps(change["edits"]),
            json.dumps(a.given.research),
            int(change["external_input"]),
            "withheld" if withheld else "proposed",
            committed.withheld,
            None if withheld else adopt.branch_name(run_id),
            committed.sha,
            committed.patch,
            json.dumps({"position": c["position"], **json.loads(c["signals"])}),
            a.given.repo.revision,
            a.now,
        ),
    )
    return change["id"]


def _store_learning(conn: sqlite3.Connection, cfg: dict, run_id: str, a: Authored):
    c, out, given = a.candidate, a.output, a.given
    if out["changes"] or len(out["learning"]) != 1:
        raise Rejected(
            "a learning answer holds exactly one recommendation and no change"
        )
    rec = out["learning"][0]
    if not rec["evidence"]:
        raise Rejected("the recommendation cites no evidence")
    _given(rec["evidence"], given.evidence, "the recommendation")
    unknown = sorted(set(rec["research"]) - set(given.research))
    if unknown:
        raise Rejected(
            "the recommendation uses research findings that were not given: "
            + ", ".join(unknown)
        )
    knowledge.check_evidence(conn, rec["evidence"])
    support = knowledge.item_support(conn, "learning", rec["evidence"])
    if not knowledge.support_holds(support, cfg, "learning"):
        raise Rejected(
            "the recommendation rests on too few of the user's own requests (15.1)"
        )
    if not rec["summary"].strip() or not rec["next_step"].strip():
        raise Rejected("the recommendation needs a summary and a next step")
    scope = knowledge.resolve_scope(conn, rec["scope"], rec["evidence"])
    learning_id = knowledge.new_id("l", run_id, c["id"])
    conn.execute(
        "INSERT INTO learning (id, run, scope, summary, evidence, research,"
        " next_step, outcome, disposition, pattern_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (
            learning_id,
            run_id,
            knowledge.scope_key(scope),
            rec["summary"].strip(),
            json.dumps(sorted(set(rec["evidence"]))),
            json.dumps(sorted(set(rec["research"]))),
            rec["next_step"].strip(),
            rec["helps_achieve"].strip(),
            c["pattern_id"],
            a.now,
            a.now,
        ),
    )
    return learning_id

"""Adoption: dream branches, commits, checks, merge, deploy and rollback (9.x).

Each repository with changes gets a work copy under
`~/.dreaming/work/<run>/<repo>`, a `git worktree` on the branch `dream/<run>`
made from the repository's default branch, so the user's checkout is never
touched while the changes are written (9.1). Work copies are removed when the
run ends; branches stay.

The author edits the work copy with its file tools during its call. The
program then commits the edits as one commit per change, after running the
repository's generate commands so derived files are in the same commit (9.2,
9.5), and after the public scan for a public repository (9.12). A change that
fails the scan or its generate commands is saved as a patch under
`~/.dreaming/patches/<run>/` and withheld instead.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from itertools import groupby
from pathlib import Path

from dreaming import config, knowledge, scan
from dreaming.backends import ProcessResult, run_process
from dreaming.errors import DreamingError, Rejected

BRANCH_PREFIX = "dream/"
# Marks the program's own recovery reverts, which are failed adoption rather
# than the user's rejection (9.13).
REVERT_TRAILER = "Dreaming-Revert"
# Names the change a commit holds, so a resumed step finds its commit.
CHANGE_TRAILER = "Dreaming-Change"
# Names the run whose tested merge commit this is.
RUN_TRAILER = "Dreaming-Run"
# Steps after which a repository's checkout holds the dream's merge.
MERGED_STEPS = ("merged", "deploying", "deployed", "reverted")


def _git(
    repo: Path, *args: str, check: bool = True, input: str | None = None
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        input=input,
    )
    if check and result.returncode != 0:
        raise DreamingError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result


def branch_name(run_id: str) -> str:
    return f"{BRANCH_PREFIX}{run_id}"


def work_root(home: Path, run_id: str) -> Path:
    return home / "work" / run_id


def repo_path(cfg: dict, name: str) -> Path:
    return Path(cfg["repositories"][name]["path"]).expanduser()


def run_command(command: str, cwd: Path, cfg: dict) -> ProcessResult:
    """A configured generate, lint, test or deploy command, run by the shell in
    its own process group and killed after the command timeout."""
    return run_process(
        ["/bin/sh", "-c", command],
        cwd=cwd,
        timeout=cfg["adoption"]["command_timeout_seconds"],
    )


def _failed(command: str, result: ProcessResult) -> str:
    if result.timed_out:
        return f"{command!r} timed out"
    output = (result.stderr.strip() or result.stdout.strip()).splitlines()
    tail = f": {output[-1]}" if output else ""
    return f"{command!r} exited {result.returncode}{tail}"


def branch_exists(repo: Path, branch: str) -> bool:
    found = _git(
        repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    )
    return found.returncode == 0


def is_ancestor(repo: Path, commit: str, of: str) -> bool:
    """Whether `commit` is in the history of `of`; git, not a stored row, says
    whether a merge happened (design, Adoption step 4)."""
    found = _git(repo, "merge-base", "--is-ancestor", commit, of, check=False)
    return found.returncode == 0


# The dream branch and its commits (9.1, 9.2, 9.5, 9.12)


@dataclass(frozen=True)
class WorkCopy:
    """A run's copy of one repository: a worktree on the dream branch, the only
    directory the author may write (9.1, 9.4)."""

    repo: str
    path: Path
    branch: str

    def clean(self) -> None:
        """Discard every change since the branch's last commit."""
        _git(self.path, "reset", "-q", "--hard", "HEAD")
        _git(self.path, "clean", "-q", "-fd")

    @property
    def checked(self) -> Path:
        """Names the change whose edits passed the check against its declared
        paths, so a resume after the generate commands ran does not mistake
        derived files for undeclared edits."""
        return self.path.with_name(f"{self.path.name}.checked")


def work_path(home: Path, run_id: str, repo: str) -> Path:
    return work_root(home, run_id) / repo


def work_copy(home: Path, cfg: dict, run_id: str, repo: str) -> WorkCopy:
    """The run's work copy of `repo`, made on first use on the branch
    `dream/<run>` from the default branch's current commit. Making it writes
    only the work copy and git's own records; the user's checkout, its index
    and its branch are untouched."""
    path = repo_path(cfg, repo)
    work = work_path(home, run_id, repo)
    branch = branch_name(run_id)
    if not (work / ".git").exists():
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(path, "worktree", "prune")
        ref = f"refs/heads/{branch}"
        if _git(path, "rev-parse", "--verify", "--quiet", ref, check=False).stdout:
            _git(path, "worktree", "add", "-q", str(work), branch)
        else:
            default = cfg["repositories"][repo]["default_branch"]
            _git(path, "worktree", "add", "-q", "-b", branch, str(work), default)
    return WorkCopy(repo, work, branch)


@dataclass(frozen=True)
class Committed:
    """A change's outcome on the dream branch: its commit, or why it was
    withheld and where its patch was saved."""

    sha: str | None = None
    withheld: str | None = None
    patch: str | None = None


def patch_path(home: Path, run_id: str, change_id: str) -> Path:
    return home / "patches" / run_id / f"{change_id}.patch"


def _commit_of(work: WorkCopy, change_id: str) -> str | None:
    found = _git(
        work.path,
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep={CHANGE_TRAILER}: {change_id}",
        "HEAD",
    )
    return found.stdout.split()[0] if found.stdout.strip() else None


def _staged(work: WorkCopy) -> set[str]:
    _git(work.path, "add", "-A")
    names = _git(work.path, "diff", "--cached", "--name-only", "--no-renames", "-z")
    return {name for name in names.stdout.split("\0") if name}


def _withhold(home: Path, run_id: str, change_id: str, work: WorkCopy, message: str):
    """Save the staged change with its message as a patch outside every
    repository. The work copy keeps the change until the next author call
    cleans it, so a resume reaches the same outcome."""
    diff = _git(work.path, "diff", "--cached", "--binary").stdout
    path = patch_path(home, run_id, change_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(f"{message}\n---\n{diff}")
    os.replace(partial, path)
    return path


def commit_change(
    home: Path,
    cfg: dict,
    run_id: str,
    work: WorkCopy,
    change_id: str,
    message: str,
    paths: set[str],
    quotes: list[tuple[str, str]],
) -> Committed:
    """Commit the author's edits in the work copy as the change's one commit.

    The edited paths must be exactly the paths the change declares; otherwise
    it is `Rejected`. The repository's generate commands then run, and for a
    public repository the scan checks the staged change and its message. A
    failure of either withholds the change as a patch. Calling this again for a
    change already committed returns its commit.
    """
    sha = _commit_of(work, change_id)
    if sha is not None:
        return Committed(sha=sha)
    entry = cfg["repositories"][work.repo]
    edited = _staged(work)
    if not (work.checked.is_file() and work.checked.read_text() == change_id):
        if not edited:
            raise Rejected("the author made no edit in the work copy")
        if edited != paths:
            raise Rejected(
                "the work copy's changes do not match the edits: "
                f"changed {', '.join(sorted(edited))}; "
                f"declared {', '.join(sorted(paths))}"
            )
        work.checked.write_text(change_id)
    message = f"{message.rstrip()}\n{CHANGE_TRAILER}: {change_id}\n"
    problems = []
    for command in entry["generate"]:
        result = run_command(command, work.path, cfg)
        if result.returncode != 0 or result.timed_out:
            problems.append(f"the generate command {_failed(command, result)}")
            break
    _staged(work)
    if not problems and entry["public"]:
        diff = _git(work.path, "diff", "--cached", "--no-color", "--no-ext-diff")
        problems = scan.scan(
            work.path,
            "HEAD",
            diff.stdout,
            message,
            names=config.load_names(cfg),
            quotes=quotes,
            quote_chars=cfg["scan"]["quote_chars"],
        )
    if problems:
        path = _withhold(home, run_id, change_id, work, message)
        return Committed(withheld="; ".join(problems), patch=str(path))
    _git(work.path, "commit", "-q", "--no-verify", "-F", "-", input=message)
    return Committed(sha=_git(work.path, "rev-parse", "HEAD").stdout.strip())


# Linked groups (9.10)


def linked_groups(conn: sqlite3.Connection, run_id: str) -> list[list[str]]:
    """Repositories adopted together: those whose changes in this run depend on
    each other, directly or through others. A repository linked to none is a
    group of one."""
    parent: dict[str, str] = {}

    def find(repo: str) -> str:
        parent.setdefault(repo, repo)
        while parent[repo] != repo:
            parent[repo] = parent[parent[repo]]
            repo = parent[repo]
        return repo

    rows = conn.execute("SELECT repo, depends_on FROM changes WHERE run = ?", (run_id,))
    for row in rows:
        find(row["repo"])
        for other in json.loads(row["depends_on"]):
            parent[find(other)] = find(row["repo"])
    groups: dict[str, list[str]] = {}
    for repo in parent:
        groups.setdefault(find(repo), []).append(repo)
    return sorted(sorted(group) for group in groups.values())


# The Adopt step: checks, merge, deploy and rollback (9.6–9.11, 9.15)

PLAN_KEY = "adopt"


def _record(
    conn: sqlite3.Connection,
    run_id: str,
    repo: str,
    step: str,
    now: str,
    *,
    head_before: str | None = None,
    head_after: str | None = None,
    detail: str | None = None,
) -> None:
    """One adoption step, written before the work it names so that a crash
    leaves a record; whether a merge happened is still read from git."""
    conn.execute(
        "INSERT INTO adoption (run, repo, branch, step, head_before, head_after,"
        " detail, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, repo, branch_name(run_id), step, head_before, head_after, detail, now),
    )


def unfinished_adoption(
    conn: sqlite3.Connection, cfg: dict, repo: str, run_id: str
) -> dict | None:
    """An earlier dream's merge into `repo` whose deploy never finished: no
    later dream starts a branch or merge there until the user finishes or
    undoes it (design, Adoption)."""
    row = conn.execute(
        "SELECT run FROM adoption WHERE repo = ? AND run != ? ORDER BY id DESC LIMIT 1",
        (repo, run_id),
    ).fetchone()
    if row is None:
        return None
    for merged in merged_repositories(conn, cfg, row["run"]):
        if merged["repo"] == repo and merged["state"] in ("merged", "deploying"):
            return {**merged, "run": row["run"]}
    return None


def held(conn: sqlite3.Connection, run_id: str, repo: str) -> str | None:
    """Why the run's branch in `repo` stays unmerged whatever its checks say:
    a change on it that is never auto-applied (9.19, 16.3)."""
    rows = conn.execute(
        "SELECT id, external_input, currency_finding_id FROM changes"
        " WHERE run = ? AND repo = ? AND status = 'proposed' ORDER BY id",
        (run_id, repo),
    )
    for row in rows:
        if row["currency_finding_id"] is not None:
            return f"change {row['id']} is a currency change, never auto-applied (16.3)"
        if row["external_input"]:
            return (
                f"change {row['id']} received fetched web or pull-request content "
                "(9.19)"
            )
    return None


def unsupported(
    conn: sqlite3.Connection, cfg: dict, run_id: str, repo: str
) -> str | None:
    """Why the run's branch in `repo` stays unmerged: a change on it whose
    support no longer meets 4.5 or 13.3, because a session it cites was
    forgotten or its evidence dismissed after it was authored (2.11)."""
    rows = conn.execute(
        "SELECT id FROM changes WHERE run = ? AND repo = ? AND status = 'proposed'"
        " AND currency_finding_id IS NULL ORDER BY id",
        (run_id, repo),
    )
    for row in rows:
        if knowledge.change_withheld(conn, row["id"], cfg):
            return (
                f"change {row['id']} lost the support it was authored on: a "
                "session it cites was forgotten or its evidence dismissed (2.11)"
            )
    return None


# Changes replayed before their checks (9.16)

# Skills and rules, replayed on the opening prompts of past sessions.
SKILLS_AND_RULES = (
    "skills/*",
    "*/skills/*",
    "rules/*",
    "*/rules/*",
    "CLAUDE.md",
    "*/CLAUDE.md",
    "AGENTS.md",
    "*/AGENTS.md",
)
# Lenses and role prompts, replayed by re-running the role they belong to.
LENSES = "instructions/lenses.toml"
ROLE_PROMPT = re.compile(r"instructions/roles/(\w+)\.md")
OPENING = "opening"


def _instruction_repository(cfg: dict, repo: str) -> bool:
    """Whether `repo` holds the instruction set a dream runs with (7.1)."""
    configured = cfg["instructions"]["repo"]
    if not configured:
        return False
    found = Path(configured).expanduser().resolve()
    return repo_path(cfg, repo).resolve() == found


def replay_target(
    conn: sqlite3.Connection, cfg: dict, change: sqlite3.Row
) -> str | None:
    """What a change's replay runs: `opening` for a skill or rule, replayed on
    past sessions' opening prompts; the role whose prompt or lenses it changes,
    re-run on its earlier inputs; or None when the change modifies no
    instruction or cites no session observation (9.16). Lenses and role
    prompts are the instruction repository's; the same paths elsewhere are
    ordinary files."""
    cited = conn.execute(
        "SELECT 1 FROM observations"
        " WHERE evidence_id IN (SELECT value FROM json_each(?)) LIMIT 1",
        (change["evidence"],),
    ).fetchone()
    if cited is None:
        return None
    paths = [edit["path"] for edit in json.loads(change["edits"])]
    changed = []
    if _instruction_repository(cfg, change["repo"]):
        changed = sorted(
            {"reader" for path in paths if path == LENSES}
            | {m.group(1) for path in paths if (m := ROLE_PROMPT.fullmatch(path))}
        )
    if changed:
        # A role whose inputs cannot be rebuilt sorts before the reader's.
        return next((role for role in changed if role != "reader"), "reader")
    if any(fnmatchcase(path, glob) for path in paths for glob in SKILLS_AND_RULES):
        return OPENING
    return None


@dataclass
class Replayed:
    """A change's replay: one row per session for `replays`, and why the change
    is held, if it is (9.17)."""

    rows: list[dict] = field(default_factory=list)
    held: str | None = None


@dataclass
class Result:
    """What adoption did in one repository: `applied`, `waiting` (the branch
    stays unmerged, with the condition not met), `failed` (merged, then
    reverted) or `merged` (merged before an interruption, not deployed)."""

    state: str
    reason: str | None = None


@dataclass
class _Repo:
    name: str
    path: Path
    entry: dict
    base: str = ""  # the default branch's commit the merge was built on
    tested: str = ""  # the merge commit the checks ran on


@dataclass
class Adopted:
    now: str
    results: dict[str, Result]
    replays: dict[str, list[dict]] = field(default_factory=dict)


class AdoptPhase:
    """The `adopt` step plans one `adopt:<repos>` step per linked group once
    every Author step has finished. Each group step checks every repository's
    merged result, and only when every repository meets every condition merges
    and deploys them together, reverting all of them if any merge or deploy
    fails (9.6–9.11). It makes no model calls, so it runs after the budget is
    exhausted too.

    Before its checks, each change to an instruction is replayed by `replay`
    (`replay.ReplayGate`), which returns a `Replayed`; one held change keeps the
    whole branch unmerged (9.16, 9.17). Without a `replay`, such a change is
    held.

    One instance serves one process. A merge into a checkout that this process
    did not make was made before an interruption, and the run then makes no
    further merges or deploys (9.15).
    """

    def __init__(self, replay=None) -> None:
        self.replay = replay
        self.merged_here: set[str] = set()
        self.stopped: str | None = None
        self.replayed: dict[str, list[dict]] = {}

    def prepare(self, ctx, step):
        if step.key == PLAN_KEY:
            return None
        self.replayed = {}
        results = self._adopt(ctx, json.loads(step.input_ref))
        return Adopted(ctx.runner.now(), results, self.replayed)

    def apply(self, conn: sqlite3.Connection, step, prepared):
        from dreaming.run import Outcome
        from dreaming.steps import NewStep

        if step.key == PLAN_KEY:
            ready = {
                row[0]
                for row in conn.execute(
                    "SELECT repo FROM changes WHERE run = ? AND status = 'proposed'"
                    " AND commit_sha IS NOT NULL",
                    (step.run,),
                )
            }
            groups = [g for g in linked_groups(conn, step.run) if ready & set(g)]
            return Outcome(
                follow_up=[
                    NewStep(
                        f"{PLAN_KEY}:{'+'.join(group)}",
                        "adopt",
                        input_ref=json.dumps(group),
                    )
                    for group in groups
                ]
            )
        now = prepared.now
        for change_id, rows in prepared.replays.items():
            for r in rows:
                conn.execute(
                    "INSERT INTO replays (run, change_id, source, session_id, backend,"
                    " model, recurs_without, judgement, tokens, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        step.run,
                        change_id,
                        r["source"],
                        r["session_id"],
                        r["backend"],
                        r["model"],
                        int(r["recurs_without"]),
                        r["judgement"],
                        r["tokens"],
                        now,
                    ),
                )
        for repo, result in prepared.results.items():
            status = {"merged": "waiting"}.get(result.state, result.state)
            conn.execute(
                "UPDATE changes SET status = ?, reason = ?,"
                " applied_at = CASE WHEN ? = 'applied' THEN ? END,"
                " reverted_at = CASE WHEN ? = 'failed' THEN ? END"
                " WHERE run = ? AND repo = ? AND status = 'proposed'",
                (status, result.reason, status, now, status, now, step.run, repo),
            )
        states = sorted(f"{r} {v.state}" for r, v in prepared.results.items())
        return Outcome(reason=", ".join(states))

    def _adopt(self, ctx, group: list[str]) -> dict[str, Result]:
        conn, cfg, run_id = ctx.conn, ctx.cfg, ctx.run_id
        ready = [
            name
            for name in group
            if conn.execute(
                "SELECT 1 FROM changes WHERE run = ? AND repo = ?"
                " AND status = 'proposed' AND commit_sha IS NOT NULL",
                (run_id, name),
            ).fetchone()
        ]
        merged = merged_repositories(conn, cfg, run_id)
        if any(m["repo"] not in self.merged_here for m in merged):
            return _after_interruption(ready, merged)
        if self.stopped is not None:
            return {
                name: Result("waiting", f"adoption stopped: {self.stopped}")
                for name in ready
            }
        repos = [
            _Repo(name, repo_path(cfg, name), cfg["repositories"][name])
            for name in ready
        ]
        reasons: dict[str, str] = {}
        withheld = conn.execute(
            "SELECT id, repo FROM changes WHERE run = ? AND status = 'withheld'"
            " AND repo IN (SELECT value FROM json_each(?)) ORDER BY id",
            (run_id, json.dumps(group)),
        ).fetchone()
        for repo in repos:
            if withheld is not None and len(group) > 1:
                reasons[repo.name] = (
                    f"the linked change {withheld['id']} in {withheld['repo']} "
                    "was withheld"
                )
                continue
            reason = self._preflight(ctx, repo)
            if reason is not None:
                reasons[repo.name] = reason
        if reasons:
            first = next(iter(reasons.items()))
            return {
                repo.name: Result(
                    "waiting",
                    reasons.get(repo.name)
                    or f"its linked repository {first[0]} is waiting: {first[1]}",
                )
                for repo in repos
            }
        return self._merge_and_deploy(ctx, repos)

    def _preflight(self, ctx, repo: _Repo) -> str | None:
        """The first condition `repo` does not meet, or None (9.7, 9.8, 9.11)."""
        conn, cfg, run_id = ctx.conn, ctx.cfg, ctx.run_id
        earlier = unfinished_adoption(conn, cfg, repo.name, run_id)
        if earlier is not None:
            return (
                f"dream {earlier['run']} merged into it without finishing its "
                "deploy; finish or undo that adoption first"
            )
        if not cfg["adoption"]["auto_apply"]:
            return "auto-apply is disabled"
        if not (repo.entry["lint"] or repo.entry["test"]):
            return "no lint or test commands are configured (9.8)"
        return (
            held(conn, run_id, repo.name)
            or unsupported(conn, cfg, run_id, repo.name)
            or self._replays(ctx, repo)
            or self._check(ctx, repo)
            or _checkout(repo)
        )

    def _replays(self, ctx, repo: _Repo) -> str | None:
        """Replay each change to an instruction on the branch, once per
        authoring; the first held change keeps the branch unmerged (9.17)."""
        changes = ctx.conn.execute(
            "SELECT * FROM changes WHERE run = ? AND repo = ? AND status = 'proposed'"
            " ORDER BY created_at, id",
            (ctx.run_id, repo.name),
        ).fetchall()
        for change in changes:
            target = replay_target(ctx.conn, ctx.cfg, change)
            if target is None:
                continue
            if self.replay is None:
                found = Replayed(held="no replayer is set up for this dream")
            else:
                found = self.replay(ctx, change, target)
            self.replayed[change["id"]] = found.rows
            if found.held is not None:
                return f"change {change['id']} is held by its replay: {found.held}"
        return None

    def _check(self, ctx, repo: _Repo) -> str | None:
        """Build the merge of the dream branch into the default branch in a
        detached copy of its own and run every lint and test command there, from
        the merged result's own Makefiles and tests (9.6)."""
        conn, run_id, now = ctx.conn, ctx.run_id, ctx.runner.now
        default, branch = repo.entry["default_branch"], branch_name(run_id)
        check = work_path(ctx.home, run_id, repo.name).with_name(f"{repo.name}.check")
        if check.exists():
            _git(repo.path, "worktree", "remove", "--force", str(check), check=False)
            shutil.rmtree(check, ignore_errors=True)
        _git(repo.path, "worktree", "prune")
        repo.base = _git(repo.path, "rev-parse", f"refs/heads/{default}").stdout.strip()
        _record(conn, run_id, repo.name, "checking", now(), head_before=repo.base)
        check.parent.mkdir(parents=True, exist_ok=True)
        _git(repo.path, "worktree", "add", "-q", "--detach", str(check), repo.base)
        message = f"Merge dream {run_id} into {default}\n\n{RUN_TRAILER}: {run_id}\n"
        merging = _git(
            check,
            "merge",
            "-q",
            "--no-ff",
            "--no-verify",
            "-m",
            message,
            branch,
            check=False,
        )
        if merging.returncode != 0:
            return f"the dream branch does not merge cleanly into {default}"
        repo.tested = _git(check, "rev-parse", "HEAD").stdout.strip()
        for command in [*repo.entry["lint"], *repo.entry["test"]]:
            result = run_command(command, check, ctx.cfg)
            if result.returncode != 0 or result.timed_out:
                return f"the check {_failed(command, result)}"
        _record(
            conn,
            run_id,
            repo.name,
            "checked",
            now(),
            head_before=repo.base,
            head_after=repo.tested,
        )
        return None

    def _merge_and_deploy(self, ctx, repos: list[_Repo]) -> dict[str, Result]:
        conn, run_id, now = ctx.conn, ctx.run_id, ctx.runner.now
        merged: list[_Repo] = []
        failure: tuple[str, str] | None = None
        for repo in repos:
            _record(
                conn,
                run_id,
                repo.name,
                "merging",
                now(),
                head_before=repo.base,
                head_after=repo.tested,
            )
            problem = _checkout(repo) or _merge(repo)
            if problem is not None:
                failure = (repo.name, problem)
                break
            self.merged_here.add(repo.name)
            merged.append(repo)
            _record(conn, run_id, repo.name, "merged", now(), head_after=repo.tested)
        if failure is None:
            for repo in merged:
                _record(conn, run_id, repo.name, "deploying", now())
                problem = _deploy(repo, ctx.cfg)
                if problem is not None:
                    failure = (repo.name, problem)
                    break
                _record(conn, run_id, repo.name, "deployed", now())
        if failure is None:
            return {repo.name: Result("applied") for repo in repos}
        return self._roll_back(ctx, repos, merged, failure)

    def _roll_back(self, ctx, repos, merged, failure) -> dict[str, Result]:
        """Revert every merged repository of the group and deploy its restored
        version, reporting both outcomes; a recovery that fails stops adoption
        and names the repository that needs manual recovery (9.9, 9.10)."""
        failed, why = failure
        results = {}
        for repo in reversed(merged):
            cause = why if repo.name == failed else f"reverted with {failed}: {why}"
            problem = _revert(ctx, repo)
            if problem is None:
                outcome = "the merge was reverted and the restored version deployed"
            else:
                outcome = f"{problem}; {repo.name} needs manual recovery"
                self.stopped = self.stopped or outcome
            results[repo.name] = Result("failed", f"{cause}; {outcome}")
        for repo in repos:
            if repo.name not in results:
                cause = (
                    why
                    if repo.name == failed
                    else f"{failed} could not be adopted: {why}"
                )
                results[repo.name] = Result("waiting", cause)
        return results


def _checkout(repo: _Repo) -> str | None:
    """Whether the user's checkout can take the tested merge: on the default
    branch, at the commit the merge was built on, with no uncommitted changes
    to tracked files and no untracked file in the merge's way (9.7)."""
    default = repo.entry["default_branch"]
    current = _git(repo.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current.stdout.strip() != default:
        on = current.stdout.strip() or "a detached HEAD"
        return f"the checkout is on {on}, not {default}"
    if _git(repo.path, "rev-parse", "HEAD").stdout.strip() != repo.base:
        return "the checkout's HEAD moved since the checks ran"
    if _git(repo.path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "the checkout has uncommitted changes to tracked files"
    untracked = _git(repo.path, "ls-files", "--others", "--exclude-standard", "-z")
    merging = _git(repo.path, "diff", "--name-only", "-z", repo.base, repo.tested)
    in_way = sorted(
        (set(untracked.stdout.split("\0")) & set(merging.stdout.split("\0"))) - {""}
    )
    if in_way:
        return f"untracked files in the checkout are in the way: {', '.join(in_way)}"
    return None


def _merge(repo: _Repo) -> str | None:
    """Bring the tested merge commit into the checkout: what is deployed is
    what was tested (Decision 1)."""
    result = _git(repo.path, "merge", "-q", "--ff-only", repo.tested, check=False)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return "the merge into the checkout failed" + (
            f": {lines[-1]}" if lines else ""
        )
    return None


def _deploy(repo: _Repo, cfg: dict) -> str | None:
    for command in repo.entry["deploy"]:
        result = run_command(command, repo.path, cfg)
        if result.returncode != 0 or result.timed_out:
            return f"the deploy command {_failed(command, result)}"
    return None


def _revert(ctx, repo: _Repo) -> str | None:
    """Revert the merge in the checkout, regenerating before the commit, mark it
    as the program's recovery (9.13), then deploy the restored version."""
    run_id, cfg = ctx.run_id, ctx.cfg
    reverting = _git(
        repo.path, "revert", "--no-commit", "-m", "1", repo.tested, check=False
    )
    if reverting.returncode != 0:
        _git(repo.path, "revert", "--abort", check=False)
        return "reverting the merge failed"
    for command in repo.entry["generate"]:
        result = run_command(command, repo.path, cfg)
        if result.returncode != 0 or result.timed_out:
            return f"the generate command {_failed(command, result)} while reverting"
    _git(repo.path, "add", "-u")
    message = (
        f"Revert dream {run_id} in {repo.name}\n\n"
        f"This reverts commit {repo.tested}, the merge of {branch_name(run_id)}.\n\n"
        f"{REVERT_TRAILER}: {run_id}\n"
    )
    committing = _git(
        repo.path, "commit", "-q", "--no-verify", "-F", "-", input=message, check=False
    )
    if committing.returncode != 0:
        return "committing the revert failed"
    revert = _git(repo.path, "rev-parse", "HEAD").stdout.strip()
    _record(
        ctx.conn,
        run_id,
        repo.name,
        "reverted",
        ctx.runner.now(),
        head_before=repo.tested,
        head_after=revert,
    )
    problem = _deploy(repo, cfg)
    if problem is not None:
        return f"redeploying the restored version failed: {problem}"
    return None


def _after_interruption(ready: list[str], merged: list[dict]) -> dict[str, Result]:
    """The outcome of each repository when the run resumes after a merge into a
    checkout: no further merges or deploys, and each merged repository is
    reported with the commands to finish or undo it (9.15)."""
    by_repo = {m["repo"]: m for m in merged}
    results = {}
    for name in ready:
        m = by_repo.get(name)
        if m is None:
            results[name] = Result(
                "waiting",
                "the dream was interrupted after its first merge, so it makes no "
                "further merges (9.15)",
            )
        elif m["state"] == "deployed":
            results[name] = Result("applied")
        elif m["state"] == "reverted":
            results[name] = Result(
                "failed", "the merge was reverted before the dream was interrupted"
            )
        else:
            results[name] = Result(
                "merged",
                "merged before the dream was interrupted and not deployed; finish "
                "or undo the adoption (9.15)",
            )
    return results


# Merges, deletions and reverts the user made (9.13, 9.14)


def landed(repo: Path, commit: str, branch: str) -> tuple[str, str]:
    """The commit through which `commit` reached `branch`'s first-parent line,
    and that commit's date: the merge that brought it in, or `commit` itself
    when the branch was fast-forwarded to it."""
    later = _git(
        repo,
        "log",
        "--first-parent",
        "--ancestry-path",
        "--format=%H %P",
        f"{commit}..refs/heads/{branch}",
    ).stdout.split("\n")
    oldest = [line.split() for line in later if line.strip()]
    found = commit
    if oldest and oldest[-1][1] != commit:
        found = oldest[-1][0]
    date = _git(repo, "log", "-1", "--format=%cI", found).stdout.strip()
    return found, date


def detect_merges(conn: sqlite3.Connection, cfg: dict, run_id: str, now: str) -> None:
    """Record earlier dreams' changes the user merged or deleted by hand, from
    git history, so only changes still on an unmerged branch hold back their
    pattern (9.14) and appear as unmerged in the report (10.4). A change whose
    commit is in the default branch is applied, dated by the commit that
    brought it in, so its effect is measured (11.1). A change whose branch is
    gone unmerged was rejected by the user (9.13); a currency change's deleted
    branch is left to the currency review (16.6). Repositories the program
    merged into keep their own record (9.15)."""
    rows = conn.execute(
        "SELECT id, run, repo, branch, commit_sha, currency_finding_id FROM changes"
        " WHERE run != ? AND status IN ('proposed', 'waiting')"
        " AND branch IS NOT NULL AND commit_sha IS NOT NULL ORDER BY id",
        (run_id,),
    ).fetchall()
    program: dict[str, set[str]] = {}
    for row in rows:
        entry = cfg["repositories"].get(row["repo"])
        path = repo_path(cfg, row["repo"]) if entry else None
        if path is None or not path.is_dir():
            continue
        if row["run"] not in program:
            merged = merged_repositories(conn, cfg, row["run"])
            program[row["run"]] = {m["repo"] for m in merged}
        if row["repo"] in program[row["run"]]:
            continue
        default = entry["default_branch"]
        if is_ancestor(path, row["commit_sha"], f"refs/heads/{default}"):
            _, date = landed(path, row["commit_sha"], default)
            conn.execute(
                "UPDATE changes SET status = 'applied', applied_at = ?, reason = ?"
                " WHERE id = ?",
                (date, f"merged into {default} by hand", row["id"]),
            )
        elif row["currency_finding_id"] is None and not branch_exists(
            path, row["branch"]
        ):
            conn.execute(
                "UPDATE changes SET status = 'rejected', reverted_at = ?, reason = ?"
                " WHERE id = ?",
                (now, "its branch was deleted without being merged", row["id"]),
            )


def detect_reverts(conn: sqlite3.Connection, cfg: dict) -> None:
    """Record applied changes that the default branch has since reverted, from
    git history: a revert of the change's commit or of its merge. A revert
    carrying the `Dreaming-Revert` trailer is the program's recovery or a 9.15
    undo, so the change failed; any other is the user's rejection (9.13). A
    reset or a manual undo is not seen."""
    rows = conn.execute(
        "SELECT c.id, c.repo, c.commit_sha, a.head_after AS merge FROM changes c"
        " LEFT JOIN adoption a"
        " ON a.run = c.run AND a.repo = c.repo AND a.step = 'merged'"
        " WHERE c.status = 'applied' AND c.reverted_at IS NULL ORDER BY c.id"
    ).fetchall()
    for row in rows:
        entry = cfg["repositories"].get(row["repo"])
        path = repo_path(cfg, row["repo"]) if entry else None
        if path is None or not path.is_dir() or not row["commit_sha"]:
            continue
        default = entry["default_branch"]
        # A merge by hand has no adoption record; git names its merge.
        merge = row["merge"] or landed(path, row["commit_sha"], default)[0]
        log = _git(
            path,
            "log",
            "--format=%cI%x00%B%x1e",
            f"{merge}..refs/heads/{default}",
            check=False,
        )
        for record in log.stdout.split("\x1e"):
            date, _, body = record.strip().partition("\0")
            reverts = (
                f"This reverts commit {row['commit_sha']}",
                f"This reverts commit {merge}",
            )
            if not any(r in body for r in reverts):
                continue
            ours = f"\n{REVERT_TRAILER}:" in f"\n{body}"
            conn.execute(
                "UPDATE changes SET status = ?, reverted_at = ? WHERE id = ?",
                ("failed" if ours else "rejected", date, row["id"]),
            )
            break


# Finishing or undoing a merged adoption (9.15, 10.2)


def _in(path: Path, command: str) -> str:
    return f"cd {shlex.quote(str(path))} && {command}"


def finish_commands(entry: dict, path: Path) -> list[str]:
    """What finishes an adoption whose merge is in the checkout: its deploy."""
    return [_in(path, command) for command in entry["deploy"]]


def _sequence(entry: dict, path: Path, first: str, *message: str) -> list[str]:
    """A merge or revert left uncommitted, the repository's generate commands,
    the commit, then its deploy commands (10.2, Q34)."""
    repo = shlex.quote(str(path))
    commit = f"git -C {repo} commit"
    commit += "".join(f" -m {shlex.quote(m)}" for m in message) or " --no-edit"
    return [
        f"git -C {repo} {first}",
        *(_in(path, command) for command in entry["generate"]),
        f"git -C {repo} add -A",
        commit,
        *(_in(path, command) for command in entry["deploy"]),
    ]


def undo_commands(entry: dict, path: Path, merge: str, run_id: str) -> list[str]:
    """What undoes a merge: revert it, regenerate before committing, then
    deploy. The trailer marks the revert as failed adoption, not the user's
    rejection of the change (9.13)."""
    return _sequence(
        entry,
        path,
        f"revert --no-commit -m 1 {merge}",
        f"Revert dream {run_id}",
        f"{REVERT_TRAILER}: {run_id}",
    )


def merged_repositories(conn: sqlite3.Connection, cfg: dict, run_id: str) -> list:
    """The repositories whose checkout this run has merged into, each with its
    state and the commands to finish or undo the adoption (9.15).

    A `merging` row alone is checked against git: the merge happened if the
    tested commit it names is in the default branch's history.
    """
    rows = conn.execute(
        "SELECT * FROM adoption WHERE run = ? ORDER BY id", (run_id,)
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    merge: dict[str, str] = {}
    for row in rows:
        latest[row["repo"]] = row
        if row["step"] == "merging":
            merge[row["repo"]] = row["head_after"]
    merged = []
    for repo, row in sorted(latest.items()):
        entry = cfg["repositories"].get(repo)
        if entry is None or repo not in merge:
            continue
        path = repo_path(cfg, repo)
        if row["step"] not in MERGED_STEPS and not is_ancestor(
            path, merge[repo], entry["default_branch"]
        ):
            continue
        state = "merged" if row["step"] in ("merging", "merged") else row["step"]
        merged.append(
            {
                "repo": repo,
                "path": str(path),
                "merge": merge[repo],
                "state": state,
                "finish": []
                if state in ("deployed", "reverted")
                else finish_commands(entry, path),
                "undo": []
                if state == "reverted"
                else undo_commands(entry, path, merge[repo], run_id),
            }
        )
    return merged


def repository_commands(conn: sqlite3.Connection, cfg: dict, run_id: str) -> list:
    """Per repository, the run's changes with their state, and the commands to
    adopt or undo them (10.2): a merge of the dream branch while it is
    unmerged; once applied, a revert per change and one for the whole merge;
    after a recovery revert, a revert of that revert; and for a merge left
    undeployed by an interruption, the commands to finish or undo it (9.15).
    A user's revert carries no trailer, so it counts as a rejection (9.13)."""
    rows = conn.execute(
        "SELECT * FROM changes WHERE run = ? ORDER BY repo, created_at, id", (run_id,)
    ).fetchall()
    merged = {m["repo"]: m for m in merged_repositories(conn, cfg, run_id)}
    reverts = dict(
        conn.execute(
            "SELECT repo, head_after FROM adoption WHERE run = ? AND step = 'reverted'",
            (run_id,),
        ).fetchall()
    )
    found = []
    for repo, group in groupby(rows, key=lambda r: r["repo"]):
        group = list(group)
        entry = cfg["repositories"].get(repo)
        committed = [c for c in group if c["commit_sha"]]
        commands: list[tuple[str, list[str]]] = []
        m = merged.get(repo)
        if entry is None or not committed:
            pass
        elif m is None:
            first = f"merge --no-commit {branch_name(run_id)}"
            commands.append(("Merge", _sequence(entry, repo_path(cfg, repo), first)))
        elif m["state"] == "deployed":
            path = repo_path(cfg, repo)
            for c in committed:
                commands.append(
                    (
                        f"Revert {c['id']}",
                        _sequence(
                            entry,
                            path,
                            f"revert --no-commit {c['commit_sha']}",
                            f"Revert {c['summary'] or c['id']}",
                        ),
                    )
                )
            commands.append(
                (
                    "Revert the whole merge",
                    _sequence(
                        entry,
                        path,
                        f"revert --no-commit -m 1 {m['merge']}",
                        f"Revert dream {run_id}",
                    ),
                )
            )
        elif m["state"] == "reverted":
            revert = reverts[repo]
            commands.append(
                (
                    "Revert the recovery revert",
                    _sequence(
                        entry,
                        repo_path(cfg, repo),
                        f"revert --no-commit {revert}",
                        f"Reapply dream {run_id}",
                    ),
                )
            )
        else:
            commands += [("Finish", m["finish"]), ("Undo", m["undo"])]
        found.append(
            {
                "repo": repo,
                "state": m["state"] if m else "unmerged",
                "changes": [dict(c) for c in group],
                "commands": commands,
            }
        )
    return found


def describe_merged(merged: list[dict]) -> str:
    lines = []
    for m in merged:
        lines.append(f"{m['repo']} ({m['state']}, merge {m['merge']})")
        if m["finish"]:
            lines += ["  to finish:", *(f"    {c}" for c in m["finish"])]
        if m["undo"]:
            lines += ["  to undo:", *(f"    {c}" for c in m["undo"])]
    return "\n".join(lines)


# Removing a run's work copies and branches


def remove_work_copies(home: Path, cfg: dict, run_id: str) -> None:
    """Remove the run's work copies; their branches stay (design, Adoption)."""
    root = work_root(home, run_id)
    if not root.exists():
        return
    for name in cfg["repositories"]:
        work = root / name
        path = repo_path(cfg, name)
        if work.exists() and path.is_dir():
            _git(path, "worktree", "remove", "--force", str(work), check=False)
    shutil.rmtree(root, ignore_errors=True)
    for name in cfg["repositories"]:
        path = repo_path(cfg, name)
        if path.is_dir():
            _git(path, "worktree", "prune", check=False)


def remove_branches(home: Path, cfg: dict, run_id: str) -> list[dict]:
    """Delete the run's work copies and its dream branches, which no checkout
    has merged (discard, 5.4)."""
    remove_work_copies(home, cfg, run_id)
    branch = branch_name(run_id)
    removed = []
    for name in sorted(cfg["repositories"]):
        path = repo_path(cfg, name)
        if not path.is_dir():
            continue
        if not branch_exists(path, branch):
            continue
        _git(path, "branch", "-D", branch)
        removed.append({"repo": name, "branch": branch})
    return removed

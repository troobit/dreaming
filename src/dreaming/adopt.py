"""Private dream branches, tested merge commits, and recoverable local adoption."""

import hashlib
import html
import json
import os
import shlex
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from dreaming.config import ConfigError, git, instruction_revision, require_external
from dreaming.knowledge import _rows
from dreaming.redact import redact
from dreaming.scan import scan_output


def _now():
    return datetime.now(UTC).isoformat()


def _commands(cwd, commands):
    for command in commands:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            close_fds=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
        if result.returncode:
            raise ConfigError(f"command_failed: {command}: {redact(result.stderr[-1000:])}")


def _git_ok(repo, *args):
    return (
        subprocess.run(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            close_fds=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        ).returncode
        == 0
    )


def _baseline_text(repo, revision):
    result = subprocess.run(
        ["git", "-C", str(repo), "grep", "-I", "-h", ".", revision],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        close_fds=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    )
    if result.returncode not in {0, 1}:
        raise ConfigError("Cannot inspect public repository baseline")
    return result.stdout.decode("utf-8", errors="replace")


class Adoption:
    def __init__(self, store, run_id, config, *, transcript_texts=(), replay=None):
        self.store, self.run_id, self.config = store, run_id, config
        self.home = store.path.parent
        self.branch = f"dream/{run_id}"
        self.replay = replay
        self.transcripts = list(transcript_texts)
        self.transcripts += [
            r[0] for r in store.connection.execute("SELECT redacted_quote FROM observations")
        ]
        self.names = (
            require_external(config.names_file).read_text().splitlines()
            if config.names_file
            else []
        )
        self.settings = {
            str(Path(v["path"]).resolve()): v for v in config.repositories.values() if v.get("path")
        }

    def changes(self):
        return _rows(
            self.store.connection.execute(
                "SELECT * FROM changes WHERE run_id=? ORDER BY rowid", (self.run_id,)
            )
        )

    def _path(self, repo, suffix="author"):
        key = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
        root = require_external(self.home / "work" / self.run_id)
        path = root / f"{key}-{suffix}"
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ConfigError("invalid_worktree_path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def _event(self, repo, step, *, before=None, after=None, outcome=None):
        self.store.connection.execute(
            "INSERT INTO adoption(run_id,repo,branch,step,head_before,head_after,at,outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                str(repo),
                self.branch,
                step,
                before,
                after,
                _now(),
                redact(outcome) if outcome else None,
            ),
        )

    def _hold(self, repo, reason, *, withheld=False):
        self.store.connection.execute(
            "UPDATE changes SET status=CASE WHEN status='withheld' OR ? "
            "THEN 'withheld' ELSE 'waiting' END,"
            "reason=? WHERE run_id=? AND repo=? AND status NOT IN ('applied','rejected','failed')",
            (int(withheld), reason, self.run_id, str(repo)),
        )

    def _default(self, repo):
        return instruction_revision(
            replace(
                self.config,
                instruction_repo=Path(repo),
                default_branch=self.settings[str(repo)].get("default_branch"),
            )
        )

    def _pending_deploy(self, repo):
        row = self.store.connection.execute(
            "SELECT step FROM adoption WHERE repo=? AND run_id<>? ORDER BY id DESC LIMIT 1",
            (str(repo), self.run_id),
        ).fetchone()
        return row and row[0] in {"merging", "merged", "deploying"}

    def author_branches(self):
        for change in self.changes():
            repo = Path(change["repo"]).resolve()
            if str(repo) not in self.settings:
                self._hold(change["repo"], "unconfigured_repository", withheld=True)
                continue
            if self._pending_deploy(repo):
                self._hold(repo, "earlier_merge_not_deployed")
                continue
            if change["commit_sha"] or change["status"] in {"withheld", "failed", "rejected"}:
                continue
            _, base = self._default(repo)
            work = self._path(repo)
            if not work.exists():
                if _git_ok(repo, "show-ref", "--verify", f"refs/heads/{self.branch}"):
                    git(repo, "worktree", "add", str(work), self.branch)
                else:
                    git(repo, "worktree", "add", "-b", self.branch, str(work), base)
            self.store.connection.execute(
                "UPDATE changes SET branch=? WHERE id=?", (self.branch, change["id"])
            )
            # Reconcile a commit completed just before the durable row update.
            commits = git(
                work,
                "log",
                "--format=%H",
                "--fixed-strings",
                "--grep",
                f"Dreaming-Change: {change['id']}",
                f"{base}..HEAD",
            ).splitlines()
            if commits:
                self.store.connection.execute(
                    "UPDATE changes SET commit_sha=? WHERE id=?", (commits[0], change["id"])
                )
                continue
            try:
                for edit in json.loads(change["edits"]):
                    relative = PurePosixPath(edit["path"])
                    path = work / relative
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or ".git" in relative.parts
                        or not path.resolve().is_relative_to(work.resolve())
                        or path.is_symlink()
                        or not path.is_file()
                    ):
                        raise ConfigError("invalid_edit_path")
                    text = path.read_text()
                    if not edit["before"].strip() or text.count(edit["before"]) != 1:
                        raise ConfigError("changed_edit_preimage")
                    path.write_text(text.replace(edit["before"], edit["after"], 1))
                _commands(work, self.settings[str(repo)].get("generate", []))
                git(work, "add", "-A")
                patch = git(work, "diff", "--cached", "--binary", strip=False)
                message = (
                    f"[dream]: apply {change['id']}\n\nEvidence: "
                    + ", ".join(json.loads(change["evidence"]))
                    + f"\nDreaming-Change: {change['id']}\nDreaming-Run: {self.run_id}\n"
                )
                if not patch:
                    raise ConfigError("empty_change")
                if self.settings[str(repo)].get("public"):
                    added = "\n".join(
                        line[1:]
                        for line in patch.splitlines()
                        if line.startswith("+") and not line.startswith("+++")
                    )
                    existing = _baseline_text(repo, base)
                    reasons = scan_output(
                        added + "\n" + message,
                        transcripts=self.transcripts,
                        existing=existing,
                        names=self.names,
                        quote_chars=self.config.scan.get("quote_chars", 80),
                        patterns=self.config.scan.get("credential_patterns", []),
                    )
                    if "GIT binary patch" in patch:
                        reasons.append("binary_output")
                    if reasons:
                        parent = require_external(self.home / "patches" / self.run_id)
                        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                        key = hashlib.sha256(change["id"].encode()).hexdigest()
                        target = require_external(parent / f"{key}.patch")
                        target.write_text(patch)
                        target.chmod(0o600)
                        raise ConfigError("public_scan:" + ",".join(reasons))
                git(work, "commit", "-m", message)
                self.store.connection.execute(
                    "UPDATE changes SET commit_sha=? WHERE id=?",
                    (git(work, "rev-parse", "HEAD"), change["id"]),
                )
            except (ConfigError, OSError, ValueError) as exc:
                git(work, "reset", "--hard", "HEAD")
                git(work, "clean", "-fd")
                self.store.connection.execute(
                    "UPDATE changes SET status='withheld',reason=? WHERE id=?",
                    (redact(str(exc)), change["id"]),
                )

    def _preflight(self, repo, expected=None):
        branch, sha = self._default(repo)
        branch = branch.removeprefix("refs/heads/")
        if branch.startswith("refs/remotes/"):
            branch = git(repo, "symbolic-ref", branch).removeprefix("refs/remotes/")
            branch = branch.split("/", 1)[-1]
        if git(repo, "branch", "--show-current") != branch:
            raise ConfigError("checkout_not_on_default_branch")
        if git(repo, "status", "--porcelain", "--untracked-files=normal"):
            raise ConfigError("dirty_checkout")
        if expected and git(repo, "rev-parse", "HEAD") != expected:
            raise ConfigError("checkout_HEAD_moved")
        if not self.settings[str(repo)].get("lint") or not self.settings[str(repo)].get("test"):
            raise ConfigError("no_configured_lint_and_test")
        return sha

    def _check(self, repo):
        head = self._preflight(repo)
        path = self._path(repo, "check")
        if path.exists():
            git(repo, "worktree", "remove", "--force", str(path))
        self._event(repo, "checking", before=head)
        git(repo, "worktree", "add", "--detach", str(path), head)
        git(path, "merge", "--no-ff", self.branch, "-m", f"Dreaming merge: {self.run_id}")
        merged = git(path, "rev-parse", "HEAD")
        settings = self.settings[str(repo)]
        _commands(path, [*settings.get("lint", []), *settings.get("test", [])])
        if git(path, "status", "--porcelain"):
            raise ConfigError("checks_modified_merge_result")
        self._event(repo, "checked", before=head, after=merged)
        return head, merged

    def _groups(self):
        # Every change in a repository shares its branch and therefore its fate.
        rows = self.changes()
        graph = {r["repo"]: set() for r in rows}
        by_id = {r["id"]: r["repo"] for r in rows}
        for row in rows:
            for other in json.loads(row["depends_on"]):
                if other in by_id:
                    graph[row["repo"]].add(by_id[other])
                    graph[by_id[other]].add(row["repo"])
        while graph:
            pending, group = [min(graph)], set()
            while pending:
                repo = pending.pop()
                if repo not in group:
                    group.add(repo)
                    pending.extend(graph.pop(repo, ()))
            yield sorted(group)

    def _rollback(self, merged, reason):
        okay = True
        for repo, sha in reversed(merged):
            try:
                git(repo, "revert", "--no-commit", "-m", "1", sha)
                _commands(repo, self.settings[str(repo)].get("generate", []))
                git(repo, "add", "-A")
                git(
                    repo,
                    "commit",
                    "-m",
                    f"Revert Dreaming merge {sha}\n\nDreaming-Revert: {self.run_id}",
                )
                restored = git(repo, "rev-parse", "HEAD")
                try:
                    _commands(repo, self.settings[str(repo)].get("deploy", []))
                    result = "success"
                except ConfigError:
                    result, okay = "failed", False
                self._event(
                    repo,
                    "reverted",
                    before=sha,
                    after=restored,
                    outcome=f"{reason}; restored_deploy={result}",
                )
                self.store.connection.execute(
                    "UPDATE changes SET status='failed',reverted_at=?,reason=? "
                    "WHERE run_id=? AND repo=?",
                    (_now(), reason, self.run_id, str(repo)),
                )
            except (ConfigError, OSError) as exc:
                okay = False
                self._event(repo, "reverted", before=sha, outcome=f"recovery_failed:{exc}")
        return okay

    def run(self):
        stats = self.store.connection.execute(
            "SELECT statistics FROM runs WHERE id=?", (self.run_id,)
        ).fetchone()[0]
        if (
            json.loads(stats).get("resumed")
            and self.store.connection.execute(
                "SELECT 1 FROM adoption WHERE run_id=? "
                "AND step IN ('merging','merged','deploying','deployed')",
                (self.run_id,),
            ).fetchone()
        ):
            reconcile_merges(self.store, self.run_id)
            return recovery_commands(self.store, self.run_id, self.config)
        self.author_branches()
        for group in self._groups():
            checked, merged = {}, []
            try:
                for name in group:
                    repo = Path(name)
                    rows = [r for r in self.changes() if r["repo"] == name]
                    if str(repo) not in self.settings:
                        raise ConfigError("unconfigured_repository")
                    if self._pending_deploy(repo):
                        raise ConfigError("earlier_merge_not_deployed")
                    if not self.config.auto_apply:
                        raise ConfigError("auto_apply_disabled")
                    if any(r["external_input"] or r["currency_finding_id"] for r in rows):
                        raise ConfigError("external_input_requires_manual_merge")
                    if any(
                        r["status"] in {"withheld", "failed", "rejected"} or not r["commit_sha"]
                        for r in rows
                    ):
                        raise ConfigError("held_change_on_branch")
                    if self.replay:
                        for row in rows:
                            reason = self.replay(row)
                            if reason:
                                raise ConfigError(reason)
                    checked[name] = self._check(repo)
                for name, (head, _) in checked.items():
                    self._preflight(Path(name), head)
                for name, (head, sha) in checked.items():
                    repo = Path(name)
                    self._preflight(repo, head)
                    self._event(repo, "merging", before=head, after=sha)
                    git(repo, "merge", "--ff-only", sha)
                    merged.append((repo, sha))
                    self._event(repo, "merged", before=head, after=sha)
                    self.store.connection.execute(
                        "UPDATE changes SET status='applied',applied_at=?,reason=NULL "
                        "WHERE run_id=? AND repo=?",
                        (_now(), self.run_id, name),
                    )
                for repo, sha in merged:
                    self._event(repo, "deploying", after=sha)
                    _commands(repo, self.settings[str(repo)].get("deploy", []))
                    self._event(repo, "deployed", after=sha)
            except (ConfigError, OSError) as exc:
                for name in group:
                    self._hold(name, redact(str(exc)))
                if merged and not self._rollback(merged, redact(str(exc))):
                    self.store.connection.execute(
                        "UPDATE runs SET notice=? WHERE id=?",
                        ("adoption recovery failed; manual recovery required", self.run_id),
                    )
                    break
        self.cleanup()
        return recovery_commands(self.store, self.run_id, self.config)

    def cleanup(self):
        cleanup_worktrees(self.store, self.run_id)


def cleanup_worktrees(store, run_id):
    root = require_external(store.path.parent / "work" / run_id)
    repos = {
        row[0]
        for row in store.connection.execute("SELECT repo FROM changes WHERE run_id=?", (run_id,))
    }
    for repo in repos:
        try:
            entries = git(Path(repo), "worktree", "list", "--porcelain").splitlines()
            for entry in entries:
                if entry.startswith("worktree ") and Path(entry[9:]).resolve().is_relative_to(root):
                    git(Path(repo), "worktree", "remove", "--force", entry[9:])
        except ConfigError:
            continue


def detect_reverts(store, config):
    """Recognise Git's recorded revert messages, never infer an undo from a reset."""
    for settings in config.repositories.values():
        if not settings.get("path"):
            continue
        repo = Path(settings["path"])
        try:
            _, default = instruction_revision(
                replace(
                    config, instruction_repo=repo, default_branch=settings.get("default_branch")
                )
            )
            history = git(repo, "log", "--format=%H%x1f%aI%x1f%B%x1e", default)
        except ConfigError:
            continue
        for change in _rows(
            store.connection.execute(
                "SELECT * FROM changes WHERE repo=? AND status='applied'", (str(repo),)
            )
        ):
            merges = [
                row[0]
                for row in store.connection.execute(
                    "SELECT head_after FROM adoption WHERE run_id=? AND repo=? AND step='merged'",
                    (change["run_id"], str(repo)),
                )
            ]
            for entry in history.split("\x1e"):
                fields = entry.strip().split("\x1f", 2)
                if len(fields) != 3:
                    continue
                _, date, body = fields
                if "This reverts commit " not in body and "Dreaming-Revert:" not in body:
                    continue
                if any(sha and sha in body for sha in [change["commit_sha"], *merges]):
                    store.connection.execute(
                        "UPDATE changes SET status=?,reverted_at=?,reason=? WHERE id=?",
                        (
                            "failed" if "Dreaming-Revert:" in body else "rejected",
                            date,
                            "revert_detected_on_default_branch",
                            change["id"],
                        ),
                    )
                    break


def reconcile_merges(store, run_id):
    """A durable intent is not proof that Git advanced the user's checkout."""
    intents = _rows(
        store.connection.execute(
            "SELECT * FROM adoption WHERE run_id=? AND step='merging' ORDER BY id", (run_id,)
        )
    )
    for intent in intents:
        sha, repo = intent["head_after"], Path(intent["repo"])
        if sha and _git_ok(repo, "merge-base", "--is-ancestor", sha, "HEAD"):
            exists = store.connection.execute(
                "SELECT 1 FROM adoption WHERE run_id=? AND repo=? "
                "AND step='merged' AND head_after=?",
                (run_id, str(repo), sha),
            ).fetchone()
            if not exists:
                store.connection.execute(
                    "INSERT INTO adoption(run_id,repo,branch,step,head_before,head_after,"
                    "at,outcome) "
                    "VALUES (?,?,?,'merged',?,?,?,'recovered_from_git')",
                    (run_id, str(repo), intent["branch"], intent["head_before"], sha, intent["at"]),
                )
                store.connection.execute(
                    "UPDATE changes SET status='applied',applied_at=? WHERE run_id=? AND repo=?",
                    (intent["at"], run_id, str(repo)),
                )


def recovery_commands(store, run_id, config):
    result = []
    settings = {v["path"]: v for v in config.repositories.values() if v.get("path")}
    for repo, branch in store.connection.execute(
        "SELECT DISTINCT repo,branch FROM changes WHERE run_id=?", (run_id,)
    ):
        row = store.connection.execute(
            "SELECT head_after FROM adoption WHERE run_id=? AND repo=? "
            "AND step IN ('merging','merged') ORDER BY id DESC LIMIT 1",
            (run_id, repo),
        ).fetchone()
        reverted = store.connection.execute(
            "SELECT head_after FROM adoption WHERE run_id=? AND repo=? "
            "AND step='reverted' AND head_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (run_id, repo),
        ).fetchone()
        prefix = f"git -C {shlex.quote(repo)}"
        commands = settings.get(repo, {})
        generate = [f"(cd {shlex.quote(repo)} && {cmd})" for cmd in commands.get("generate", [])]
        deploy = [f"(cd {shlex.quote(repo)} && {cmd})" for cmd in commands.get("deploy", [])]
        undo = f"{prefix} revert --no-commit " + (
            shlex.quote(reverted[0])
            if reverted
            else "-m 1 " + shlex.quote(row[0])
            if row
            else "<merge-sha>"
        )
        finish = [
            f"{prefix} merge --no-commit --no-ff {shlex.quote(branch or 'dream/' + run_id)}",
            *generate,
            f"{prefix} commit --no-edit",
            *deploy,
        ]
        merged = bool(
            row and row[0] and _git_ok(Path(repo), "merge-base", "--is-ancestor", row[0], "HEAD")
        )
        result.append(
            {
                "repo": repo,
                "finish": (
                    [undo, *generate, f"{prefix} commit --no-edit", *deploy]
                    if reverted
                    else [*generate, *deploy]
                    if merged
                    else finish
                ),
                "undo": [undo, *generate, f"{prefix} commit --no-edit", *deploy],
            }
        )
    return result


def render_changes(store, run_id, config):
    rows = _rows(store.connection.execute("SELECT * FROM changes WHERE run_id=?", (run_id,)))
    if not rows:
        return "<p>No repository changes were proposed.</p>"
    commands = {r["repo"]: r for r in recovery_commands(store, run_id, config)}
    blocks = []
    for row in rows:
        text = f"{row['id']}: {row['status']}; evidence: {row['evidence']}; {row['reason'] or ''}"
        scripts = commands[row["repo"]]
        script = "\n".join(scripts["undo"] if row["applied_at"] else scripts["finish"])
        per_change = ""
        if row["applied_at"] and row["commit_sha"] and not row["reverted_at"]:
            individual = list(scripts["undo"])
            individual[0] = (
                f"git -C {shlex.quote(row['repo'])} revert --no-commit "
                f"{shlex.quote(row['commit_sha'])}"
            )
            per_change = "<pre>" + html.escape("\n".join(individual)) + "</pre>"
        blocks.append(
            f"<article><h3>{html.escape(row['repo'])}</h3><p>{html.escape(text)}</p>"
            f"{per_change}<pre>{html.escape(script)}</pre></article>"
        )
    return "".join(blocks)


def discard(store, record, *, config=None):
    from dreaming.run import kill_survivors, snapshot_config

    rid = record["id"]
    reconcile_merges(store, rid)
    if config is None and record.get("snapshot_path"):
        config = snapshot_config(Path(record["snapshot_path"]))
    if store.connection.execute(
        "SELECT 1 FROM adoption WHERE run_id=? AND step IN ('merged','deploying','deployed')",
        (rid,),
    ).fetchone():
        raise ValueError(
            "Run was merged; finish or undo adoption: "
            + json.dumps(recovery_commands(store, rid, config))
        )
    kill_survivors(record.get("process_group"))
    cleanup_worktrees(store, rid)
    for repo, branch in store.connection.execute(
        "SELECT DISTINCT repo,branch FROM changes WHERE run_id=?", (rid,)
    ):
        if branch:
            git(Path(repo), "branch", "-D", branch)
    with store.transaction() as conn:
        from dreaming.knowledge import queue_reassessment

        removed_evidence = {
            r[0] for r in conn.execute("SELECT id FROM observations WHERE run_id=?", (rid,))
        }
        queue_reassessment(conn, removed_evidence)
        selection = conn.execute("SELECT selection FROM runs WHERE id=?", (rid,)).fetchone()
        for selected in json.loads(selection[0]):
            if isinstance(selected, dict) and selected.get("source") and selected.get("session_id"):
                copies = [(selected["source"], selected["session_id"])] + [
                    (meta["source"], meta["id"]) for meta in selected.get("copies", [])
                ]
                conn.executemany(
                    "UPDATE sessions_seen SET entries_read=0,lens_versions='{}' "
                    "WHERE source=? AND session_id=?",
                    copies,
                )
        # Restore the last retained revision before removing revisions owned by this run.
        items = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT item_id FROM knowledge_revisions WHERE run_id=?", (rid,)
            )
        ]
        conn.execute("DELETE FROM knowledge_revisions WHERE run_id=?", (rid,))
        for item_id in items:
            prior = _rows(
                conn.execute(
                    "SELECT * FROM knowledge_revisions WHERE item_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (item_id,),
                )
            )
            if prior:
                values = {k: v for k, v in prior[0].items() if k not in {"item_id", "created_at"}}
                conn.execute(
                    "UPDATE knowledge_items SET "
                    + ",".join(k + "=?" for k in values)
                    + " WHERE id=?",
                    (*values.values(), item_id),
                )
            else:
                conn.execute("DELETE FROM knowledge_items WHERE id=?", (item_id,))
        conn.execute(
            "DELETE FROM session_inputs WHERE call_id IN (SELECT id FROM calls WHERE run_id=?)",
            (rid,),
        )
        conn.execute(
            "DELETE FROM replays WHERE change_id IN (SELECT id FROM changes WHERE run_id=?)", (rid,)
        )
        conn.execute(
            "DELETE FROM replay_batches WHERE change_id IN (SELECT id FROM changes WHERE run_id=?)",
            (rid,),
        )
        for table in (
            "calls",
            "steps",
            "observations",
            "file_findings",
            "research_findings",
            "changes",
            "adoption",
            "learning",
            "review_decisions",
            "currency_findings",
        ):
            conn.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
        conn.execute("UPDATE patterns SET last_reviewed_run=NULL WHERE last_reviewed_run=?", (rid,))
        conn.execute(
            "UPDATE runs SET status='discarded',process_group=NULL,"
            "ended_at=?,wait_until=NULL WHERE id=?",
            (_now(), rid),
        )
    return {"run_id": rid, "status": "discarded"}


class AdoptHandler:
    def __init__(self, store, run_id, config, *, dispatcher=None, roles=None, transcript_texts=()):
        self.store, self.run_id, self.config = store, run_id, config
        self.dispatcher, self.roles = dispatcher, roles
        self.transcript_texts = transcript_texts

    def before_apply(self, step, raw):
        if not self.store.connection.execute(
            "SELECT 1 FROM changes WHERE run_id=?", (self.run_id,)
        ).fetchone():
            return "{}"
        from dreaming.replay import ReplayGate

        gate = ReplayGate(self.store, self.run_id, self.config, self.dispatcher, self.roles)
        Adoption(
            self.store,
            self.run_id,
            self.config,
            replay=gate.check,
            transcript_texts=self.transcript_texts,
        ).run()
        return "{}"

    def apply(self, conn, step, raw):
        return ()

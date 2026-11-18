"""The replay gate (9.16–9.18).

A change to a skill or rule that cites session observations is replayed once
per authoring, before its repository's checks. Up to `replay_max_sessions`
past sessions showing its pattern, never forgotten ones and only those whose
opening prompt was recorded complete, are each answered twice by the
tool-free replayer: first with no instruction, then with the changed
instruction as the only one it sees (4.4). A reviewer-role call with the
judge's prompt decides whether the problem recurs in each answer. A session
counts as replayed only when the problem recurs without the change, and an
unclear judgement counts as recurrence.

A change to the lenses or the reader's prompt re-runs the reader on the stored
inputs of its earlier Read steps, once with the run's instructions and once
with the changed ones. Other roles' inputs are not kept, so a change to their
prompts cannot be replayed and is held (Decision 17).

A session forgotten while the replay runs is passed over before its next call
(2.11). Each call is keyed by its change and session, so a resumed Adopt step
reuses the answers already received and a change is replayed once per
authoring.

The change is held when the problem recurs with it in more than
`replay_max_recurrence` of the replayed sessions, when fewer than
`replay_min_sessions` sessions are replayed, when the remaining budget cannot
cover the replay, or when its replays do not all run on one backend (9.17).
Replay calls count against the budget like any other call (9.18).
"""

import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dreaming import adopt, knowledge, observe, roles, sessions, steps
from dreaming.backends import InputFailed, estimate_tokens
from dreaming.budget import LimitReached, tokens_used
from dreaming.errors import DreamingError

# Content markers that make a recorded opening prompt incomplete (3.4).
INCOMPLETE = ("truncated", "missing", "incomplete")


def opening_prompt(document: sessions.SessionDocument) -> str | None:
    """A session's first user entry, when it was recorded complete."""
    for entry in document.entries:
        if entry.role != "user":
            continue
        blocks = entry.content
        if any(block.get(flag) is True for block in blocks for flag in INCOMPLETE):
            return None
        return sessions.entry_text(entry).strip() or None
    return None


def verdict(rows: list[dict], cfg: dict) -> str | None:
    """Why a replay holds its change, or None when it passes (9.17)."""
    t = cfg["thresholds"]
    replayed = [r for r in rows if r["recurs_without"]]
    if len(replayed) < t["replay_min_sessions"]:
        return (
            f"{len(replayed)} of the {len(rows)} sessions run showed the problem "
            f"without the change; at least {t['replay_min_sessions']} are needed"
        )
    recurs = sum(r["judgement"] != "resolved" for r in replayed)
    if recurs / len(replayed) > t["replay_max_recurrence"]:
        return (
            f"the problem recurred with the change in {recurs} of {len(replayed)} "
            f"replayed sessions, more than {t['replay_max_recurrence']:.0%}"
        )
    return None


@dataclass
class _Plan:
    """One session's replay: the prompts without and with the change."""

    source: str
    session_id: str
    quotes: list[str]
    role: str
    without: list[tuple[str, dict]]
    with_change: list[tuple[str, dict]]


def _git_show(repo: Path, commit: str, path: str) -> str | None:
    found = subprocess.run(
        ["git", "-C", str(repo), "show", f"{commit}:{path}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return found.stdout if found.returncode == 0 else None


class ReplayGate:
    """Replays a change before its checks; `adopt.AdoptPhase(replay=...)`.

    `instructions` is the run's instruction set and `root` the snapshot's
    `instructions/` it was loaded from, which the changed files replace for a
    reader re-run. `apsis` opens past sessions.
    """

    def __init__(
        self, instructions: roles.InstructionSet, root: Path, apsis: sessions.Apsis
    ) -> None:
        self.instructions = instructions
        self.root = root
        self.apsis = apsis

    def __call__(self, ctx, change: sqlite3.Row, target: str) -> adopt.Replayed:
        cfg = ctx.cfg
        if target not in (adopt.OPENING, "reader"):
            return adopt.Replayed(
                held=f"the {target} role's earlier inputs are not kept, so a change "
                "to its prompt cannot be replayed"
            )
        try:
            plans = self._plans(ctx, change, target)
        except DreamingError as exc:
            return adopt.Replayed(held=exc.message)
        least = cfg["thresholds"]["replay_min_sessions"]
        if len(plans) < least:
            found = f"{len(plans)} session{'' if len(plans) == 1 else 's'}"
            return adopt.Replayed(
                held=f"only {found} showing the pattern can be replayed; "
                f"at least {least} are needed"
            )
        # Each prompt is sent, and about as much again comes back or is judged.
        needed = 2 * sum(
            estimate_tokens(prompt)
            for plan in plans
            for prompt, _ in [*plan.without, *plan.with_change]
        )
        left = ctx.runner.budget.tokens - tokens_used(ctx.conn, ctx.run_id)
        if needed > left:
            return adopt.Replayed(
                held=f"the remaining budget, {left} tokens, cannot cover the replay "
                f"(about {needed})"
            )
        rows: list[dict] = []
        try:
            for plan in plans:
                copies = [{"source": plan.source, "id": plan.session_id}]
                if observe.is_forgotten(ctx.conn, copies):
                    continue
                part = f"{change['id']}:{plan.source}:{plan.session_id}"
                rows.append(self._replay(ctx, change, plan, part))
        except LimitReached as limit:
            return adopt.Replayed(rows, f"the replay was cut short: {limit.reason}")
        except InputFailed as failed:
            return adopt.Replayed(rows, f"a replay call failed: {failed.message}")
        if len({backend for row in rows for backend in row.pop("backends")}) > 1:
            return adopt.Replayed(rows, "its replays did not all run on one backend")
        return adopt.Replayed(rows, verdict(rows, cfg))

    # Planning

    def _sessions(self, conn: sqlite3.Connection, change: sqlite3.Row):
        """Sessions showing the change's pattern, newest first, with their
        quotes; forgotten sessions and dismissed evidence are left out."""
        rows = conn.execute(
            f"{knowledge.COUNTED_OBSERVATIONS}"
            " AND (o.pattern_id = ? OR o.evidence_id IN"
            " (SELECT value FROM json_each(?)))"
            " ORDER BY o.observed_on DESC, o.source, o.session_id, o.evidence_id",
            (change["pattern_id"], change["evidence"]),
        ).fetchall()
        found: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            found.setdefault((row["source"], row["session_id"]), []).append(
                row["quote"]
            )
        return list(found.items())

    def _plans(self, ctx, change: sqlite3.Row, target: str) -> list[_Plan]:
        most = ctx.cfg["thresholds"]["replay_max_sessions"]
        if target == adopt.OPENING:
            instruction = self._instruction(ctx.cfg, change)
        else:
            changed = self._changed_instructions(ctx.cfg, change)
        plans = []
        for (source, session_id), quotes in self._sessions(ctx.conn, change):
            if len(plans) == most:
                break
            if target == adopt.OPENING:
                plan = self._opening(ctx.cfg, source, session_id, quotes, instruction)
            else:
                plan = self._reader(ctx, source, session_id, quotes, changed)
            if plan is not None:
                plans.append(plan)
        return plans

    def _instruction(self, cfg: dict, change: sqlite3.Row) -> str:
        """The changed skill or rule files at the change's commit: the only
        instruction the replayer sees in the run with the change."""
        repo = adopt.repo_path(cfg, change["repo"])
        texts = []
        for path in sorted({edit["path"] for edit in json.loads(change["edits"])}):
            text = _git_show(repo, change["commit_sha"], path)
            if text is not None:
                texts.append(f"# {path}\n\n{text.strip()}")
        return "\n\n".join(texts)

    def _changed_instructions(self, cfg: dict, change: sqlite3.Row):
        """The run's instruction set with the change's files in place."""
        repo = adopt.repo_path(cfg, change["repo"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "instructions"
            shutil.copytree(self.root, root)
            for edit in json.loads(change["edits"]):
                path = edit["path"]
                if not path.startswith("instructions/"):
                    continue
                text = _git_show(repo, change["commit_sha"], path)
                target = root / path.removeprefix("instructions/")
                if text is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_text(text)
            return roles.load(root, cfg)

    def _open(self, source: str, session_id: str):
        try:
            return observe.open_session(self.apsis, source, session_id)
        except DreamingError:
            return None

    def _opening(self, cfg, source, session_id, quotes, instruction) -> _Plan | None:
        document = self._open(source, session_id)
        prompt = opening_prompt(document) if document else None
        if prompt is None:
            return None
        replayer = self.instructions.roles["replayer"]
        without = replayer.prompt(roles.replayer_input(prompt))
        with_change = replayer.prompt(roles.replayer_input(prompt, instruction))
        if not roles.fits(cfg, "replayer", with_change):
            return None
        return _Plan(
            source,
            session_id,
            quotes,
            "replayer",
            [(without, replayer.schema)],
            [(with_change, replayer.schema)],
        )

    def _reader(self, ctx, source, session_id, quotes, changed) -> _Plan | None:
        """The reader's prompts for the session's latest finished Read step, as
        that run built them: its stored session reference and its scope."""
        stored = ctx.conn.execute(
            "SELECT run, input_ref FROM steps"
            " WHERE phase = 'read' AND key = ? AND state = 'done'"
            " ORDER BY rowid DESC LIMIT 1",
            (observe.read_key(source, session_id),),
        ).fetchone()
        ref = json.loads(stored["input_ref"]) if stored else None
        if not ref or "skipped" in ref:
            return None
        document = self._open(source, session_id)
        if document is None:
            return None
        scope = observe.run_request(ctx.conn, stored["run"]).scope
        session = observe.selected_session(ref, document)
        without = observe.reader_prompts(self.instructions, ctx.cfg, scope, session)
        with_change = observe.reader_prompts(changed, ctx.cfg, scope, session)
        if not without or not with_change:
            return None
        return _Plan(
            source,
            session_id,
            quotes,
            "reader",
            [(p, self.instructions.roles["reader"].schema) for p in without],
            [(p, changed.roles["reader"].schema) for p in with_change],
        )

    # Replaying

    def _replay(self, ctx, change: sqlite3.Row, plan: _Plan, part: str) -> dict:
        input_ref = json.dumps({"sessions": [[plan.source, plan.session_id]]})
        answers = {}
        keys = []
        for label, calls in (("without", plan.without), ("with", plan.with_change)):
            answers[label] = []
            for number, (prompt, schema) in enumerate(calls, start=1):
                key = f"{part}:{label}:{number}"
                keys.append(key)
                answers[label].append(
                    ctx.call(plan.role, prompt, schema, part=key, input_ref=input_ref)
                )
        judge = self.instructions.roles[roles.JUDGE]
        data = {
            "problem": {"change": change["summary"], "pattern": change["pattern_id"]},
            "evidence": plan.quotes,
            "without": answers["without"],
            "with": answers["with"],
        }
        keys.append(f"{part}:judge")
        judged = ctx.call(
            "reviewer",
            judge.prompt(data),
            judge.schema,
            part=keys[-1],
            input_ref=input_ref,
        )
        call_keys = [steps.call_key(ctx.step.key, key) for key in keys]
        answered = [steps.answered_by(ctx.conn, ctx.run_id, key) for key in call_keys]
        backend, model = answered[0]
        tokens = ctx.conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM calls"
            " WHERE run = ? AND step_key IN (SELECT value FROM json_each(?))",
            (ctx.run_id, json.dumps(call_keys)),
        ).fetchone()[0]
        return {
            "source": plan.source,
            "session_id": plan.session_id,
            "backend": backend,
            "model": model,
            "recurs_without": judged["without"] == "recurs",
            "judgement": judged["with"],
            "tokens": tokens,
            # Every replay of a change runs on one backend; the judge is apart.
            "backends": {b for b, _ in answered[:-1]},
        }

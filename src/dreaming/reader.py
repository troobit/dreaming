"""Incremental Read steps and deterministic, evidence-addressed clusters."""

import json
from dataclasses import asdict, replace
from itertools import groupby

from dreaming.backends import input_tokens
from dreaming.observe import apply_observations, canonical, verify_observations
from dreaming.sessions import (
    SessionError,
    _remember,
    mark_read,
    reader_document,
    reader_parts,
    record_selection,
)
from dreaming.steps import Step, Steps


def clusters(observations: list[dict]) -> list[dict]:
    result = []
    ordered = sorted(
        observations,
        key=lambda row: (
            row["pattern_id"],
            row.get("observed_at") or "",
            row["id"],
        ),
    )
    for pid, grouped in groupby(ordered, key=lambda row: row["pattern_id"]):
        rows = list(grouped)
        result.append(
            dict(
                pattern_id=pid,
                lens_id=rows[0]["lens_id"],
                pattern_key=rows[0]["pattern_key"],
                observations=rows,
            )
        )
    return result


def schedule_reads(store, run_id, selection) -> None:
    """Selection and source references only; source transcripts stay in apsis."""
    record_selection(store, selection)
    steps = []
    with store.transaction() as conn:
        for index, selected in enumerate(selection.sessions):
            for meta in selected.copies:
                _remember(conn, meta)
            key = f"read:{index}:{selected.start_entry}"
            steps.append(
                Step(
                    key,
                    "Read",
                    input_ref=canonical(
                        {
                            "session": index,
                            "start": selected.start_entry,
                        }
                    ),
                )
            )
    Steps(store, run_id).add(*steps, Step("review", "Review", tuple(s.key for s in steps)))


class ReadHandler:
    role = "reader"

    def __init__(self, store, run_id, config, roles, sessions):
        self.store, self.run_id, self.config, self.roles, self.sessions = (
            store,
            run_id,
            config,
            roles,
            tuple(sessions),
        )
        self.context_limit = None

    def resize(self, context_limit):
        self.context_limit = context_limit

    def selected(self, step):
        ref = json.loads(step.input_ref)
        return replace(self.sessions[ref["session"]], start_entry=ref["start"])

    def request(self, step):
        selected = self.selected(step)
        lenses = [asdict(lens) for lens in selected.lenses]
        limits = {
            n: min(
                self.config.backends[n].context_tokens,
                self.context_limit or self.config.backends[n].context_tokens,
            )
            for n in self.config.roles["reader"]
        }
        config = replace(
            self.config,
            backends={
                name: replace(backend, context_tokens=limits.get(name, backend.context_tokens))
                for name, backend in self.config.backends.items()
            },
        )
        context = min(limits.values())
        output = min(4096, context // 4)
        # JSON serialization in RoleSet and the dispatcher uses escaped strings
        # and a larger envelope than split_session; reserve space for both.
        base = self.roles.build("reader", sessions=[], lenses=lenses, context_limits=limits)
        overhead = base.estimated_tokens + 128
        while True:
            parts = reader_parts(
                self.store, selected, config, prompt_tokens=overhead, output_tokens=output
            )
            if not parts:
                end, payload = len(selected.document.entries), None
                request = base
                break
            part = parts[0]
            end, payload = part.end_entry, json.loads(part.payload)
            try:
                request = self.roles.build(
                    "reader", sessions=[payload], lenses=lenses, context_limits=limits
                )
                break
            except ValueError:
                overhead += max(128, input_tokens(canonical(payload), {}, []) // 4)

        return request, selected, payload, end

    def prepare(self, step):
        request, selected, payload, end = self.request(step)

        def checkpoint(response, backend):
            candidates = json.loads(response.raw_output)["observations"]
            visible = {e["location"] for e in payload["entries"]} if payload else set()
            in_part, outside = [], []
            for candidate in candidates:
                (in_part if candidate["source_location"] in visible else outside).append(candidate)
            verified = verify_observations(
                self.store,
                self.config,
                selected,
                in_part,
                backend=backend,
                model=response.model or self.config.backends[backend].model,
            )
            verified["rejected"].extend(["outside_part"] * len(outside))
            verified["end"] = end
            return canonical(verified)

        return {
            **request.metadata(),
            "checkpoint": checkpoint,
            "sessions": tuple((m.source, m.id) for m in selected.copies),
        }

    def failed(self, conn, step, reason):
        selected = self.selected(step)
        for meta in selected.copies:
            conn.execute(
                "UPDATE sessions_seen SET state='failed',reason=?,lens_versions=? "
                "WHERE source=? AND session_id=?",
                (
                    reason,
                    canonical({lens.id: lens.version for lens in selected.lenses}),
                    meta.source,
                    meta.id,
                ),
            )

    def apply(self, conn, step, raw):
        checkpoint = json.loads(raw)
        selected = self.selected(step)
        try:
            reader_document(self.store, selected, self.config)
        except SessionError as exc:
            if exc.code not in {"forgotten", "forgotten_input"}:
                raise
            return ()
        apply_observations(self.store, conn, self.run_id, selected, self.config, checkpoint)
        if checkpoint["end"] < len(selected.document.entries):
            ref = json.loads(step.input_ref)
            ref["start"] = checkpoint["end"]
            key = f"read:{ref['session']}:{ref['start']}"
            review = conn.execute(
                "SELECT depends_on FROM steps WHERE run_id=? AND key='review'", (self.run_id,)
            ).fetchone()
            dependencies = json.loads(review[0])
            conn.execute(
                "UPDATE steps SET depends_on=? WHERE run_id=? AND key='review'",
                (canonical([*dependencies, key]), self.run_id),
            )
            return (Step(key, "Read", (step.key,), canonical(ref)),)
        mark_read(self.store, selected, self.config, conn=conn)
        return ()

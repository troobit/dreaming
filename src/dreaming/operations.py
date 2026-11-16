"""Public lifecycle operations; run mutations take the engine's continuous lock."""

import json
import logging
import os
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from dreaming.config import ROLES, ConfigError, git, home_path
from dreaming.run import RunSession, inspect_run
from dreaming.store import Store


def _run_id(request):
    value = request.get("run_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid run_id")
    return value


def _record(home, request):
    record = inspect_run(home, _run_id(request))
    if record is None:
        from dreaming.api import APIError

        raise APIError("not_found", "Unknown run " + request["run_id"])
    return record


def _backends(config, value):
    if not isinstance(value, dict) or any(
        role not in ROLES
        or not isinstance(names, list)
        or not names
        or any(not isinstance(n, str) or n not in config.backends for n in names)
        for role, names in value.items()
    ):
        raise ValueError("Invalid backend overrides")
    return value


def lifecycle(config, request, *, home=None):
    from dreaming.api import APIError, _query_scope, _required_text

    home = home or home_path()
    op = request["op"]
    if op == "dream.status":
        record = _record(home, request) if request.get("run_id") else inspect_run(home)
        if record is None:
            raise APIError("not_found", "No recorded dream")
        if request.get("plan"):
            from dreaming.plan import render_plan

            with Store.readonly(home) as store:
                record["plan_path"] = str(render_plan(store, record["id"]))
        report = home / "reports" / f"{record['id']}.html"
        return {"run": record, "report_path": str(report) if report.exists() else None}
    if op not in {"dream.start", "dream.resume", "dream.discard"}:
        raise APIError("unknown_operation", f"Unknown operation {op}")
    allowed = {"dreaming_api", "op", "run_id", "backends"}
    if op == "dream.start":
        allowed |= {
            "scope",
            "project",
            "domain",
            "workflow",
            "since",
            "until",
            "reread",
            "requester",
            "budget",
            "fallback_on_usage_limit",
            "open_browser",
            "dry_run",
            "kind",
            "targets",
        }
    if request.keys() - allowed:
        raise ValueError("Unknown lifecycle fields: " + ", ".join(sorted(request.keys() - allowed)))
    if op == "dream.start":
        scope = _query_scope(config, request)
        from dreaming.sessions import SessionScope, _timestamp

        if any(
            k in request and (not isinstance(request[k], str) or not request[k])
            for k in ("since", "until")
        ):
            raise APIError("invalid_scope", "Dates must be nonempty timestamps")
        lower, upper = (_timestamp(request.get(k)) for k in ("since", "until"))
        if any(request.get(k) and v is None for k, v in (("since", lower), ("until", upper))) or (
            lower and upper and lower >= upper
        ):
            raise APIError("invalid_scope", "Invalid date range")
        for field in ("reread", "fallback_on_usage_limit", "open_browser", "dry_run"):
            if field in request and type(request[field]) is not bool:
                raise ValueError(field + " must be boolean")
        requester = _required_text(request.get("requester", "user"), "requester")
        kind = request.get("kind", "standard")
        if kind not in {"standard", "currency"}:
            raise ValueError("kind must be standard or currency")
        targets = request.get("targets")
        if kind == "currency":
            if request.get("dry_run"):
                raise ValueError("dry_run does not apply to a currency dream")
            known = {t["name"] for t in config.currency_targets}
            if not known or (
                targets is not None
                and (not isinstance(targets, list) or not targets or set(targets) - known)
            ):
                raise APIError("invalid_scope", "Unknown or unconfigured currency targets")
        elif targets is not None:
            raise ValueError("targets apply only to a currency dream")
        overrides = {
            **(
                {"kind": kind, **({"targets": targets} if targets else {})}
                if kind != "standard"
                else {}
            ),
            "scope": scope,
            **{
                k: request[k]
                for k in ("since", "until", "reread", "open_browser", "fallback_on_usage_limit")
                if k in request
            },
        }
        choices = _backends(config, request.get("backends", {}))
        if "budget" in request:
            from dreaming.config import Budget, _settings

            config = replace(config, budget=_settings(Budget, request["budget"]))
            overrides["budget"] = request["budget"]
        if "fallback_on_usage_limit" in request:
            config = replace(
                config,
                thresholds=replace(
                    config.thresholds, fallback_on_usage_limit=request["fallback_on_usage_limit"]
                ),
            )
        config = replace(
            config, roles={**config.roles, **{k: tuple(v) for k, v in choices.items()}}
        )
        if request.get("dry_run"):
            from dreaming.plan import dry_run

            return dry_run(
                config,
                home,
                scope=SessionScope(**scope),
                **{k: request[k] for k in ("since", "until", "reread") if k in request},
            )
    with RunSession(home) as session:
        if op == "dream.discard":
            record = _record(home, request)
            from dreaming.adopt import discard

            return discard(session.store, record)
        if op == "dream.resume":
            record = _record(home, request)
            latest = inspect_run(home, unfinished=True)
            if record["status"] not in {"running", "waiting", "partial", "interrupted"} or (
                latest is None or latest["id"] != record["id"]
            ):
                raise ValueError("Run is not resumable")
            prepared = session.prepare(
                config,
                revision=record["revision"],
                backend_overrides=request.get("backends"),
                allow_new=False,
            )
            if prepared.id != record["id"]:
                raise ConfigError("Run budget is exhausted")
        else:
            old = inspect_run(home, unfinished=True)
            if old:
                if request.keys() - {"dreaming_api", "op", "requester", "backends"}:
                    raise ValueError("An interrupted dream can resume with backend overrides only")
                prepared = session.prepare(
                    config, revision=old["revision"], backend_overrides=choices
                )
            else:
                revision = os.environ.get("DREAMING_REVISION")
                if not revision:
                    try:
                        revision = git(Path(__file__).resolve().parents[2], "rev-parse", "HEAD")
                    except ConfigError:
                        from dreaming import __version__

                        revision = "package-" + __version__.replace(".", "-")
                prepared = session.prepare(
                    config, revision=revision, requester=requester, overrides=overrides
                )
        session.detach("dreaming.operations:work", prepared.id)
        recovery = {}
        if session.store.connection.execute(
            "SELECT 1 FROM adoption WHERE run_id=? "
            "AND step IN ('merging','merged','deploying','deployed')",
            (prepared.id,),
        ).fetchone():
            from dreaming.adopt import recovery_commands

            recovery["recovery_commands"] = recovery_commands(
                session.store, prepared.id, prepared.config
            )
        return {
            "run_id": prepared.id,
            "resumed": prepared.resumed,
            "report_path": str(home / "reports" / f"{prepared.id}.html"),
            **recovery,
        }


def work(store, run_id):
    """Detached entry point. Rebuild services from the immutable run snapshot."""
    from dreaming.authoring import AuthorHandler, ResearchHandler
    from dreaming.backends import Dispatcher
    from dreaming.backends.claude import ClaudeBackend
    from dreaming.backends.codex import CodexBackend
    from dreaming.backends.lmstudio import LMStudioBackend
    from dreaming.inventory import inventory_section
    from dreaming.observe import entry_text
    from dreaming.reader import ReadHandler, schedule_reads
    from dreaming.report import write_report
    from dreaming.review import ReviewHandler
    from dreaming.roles import RoleSet
    from dreaming.run import Engine, snapshot_config
    from dreaming.sessions import ApsisClient, SessionScope, select_sessions
    from dreaming.steps import Step

    class PipelineReview(ReviewHandler):
        def apply(self, conn, step, raw):
            followups = super().apply(conn, step, raw)
            return (*followups, Step("adopt", "Adopt", (step.key, *(s.key for s in followups))))

    record = inspect_run(store.path.parent, run_id)
    config = snapshot_config(Path(record["snapshot_path"]))
    stats = json.loads(record["statistics"])
    config = replace(
        config,
        roles={
            **config.roles,
            **{k: tuple(v) for k, v in stats.get("backend_overrides", {}).items()},
        },
    )
    overrides = json.loads(record["overrides"])
    currency = overrides.get("kind") == "currency"

    def finalize(s, rid):
        from dreaming.adopt import cleanup_worktrees, reconcile_merges, render_changes
        from dreaming.effect import render_effect

        reconcile_merges(s, rid)
        cleanup_worktrees(s, rid)
        sections = {
            "changes": render_changes(s, rid, config),
            "effect": render_effect(s, config),
        }
        try:
            reader = ApsisClient(codex_exec_dirs=(s.path.parent / "codex-exec",))
            sections["inventory"] = inventory_section(s, rid, config, reader)
        except Exception:
            logging.getLogger(__name__).warning("Tooling inventory unavailable", exc_info=True)
        write_report(s, rid, open_browser=overrides.get("open_browser", True), sections=sections)

    try:
        from dreaming.adopt import detect_reverts

        detect_reverts(store, config)
        roles = RoleSet(Path(record["snapshot_path"]), config)
        client = ApsisClient(codex_exec_dirs=(store.path.parent / "codex-exec",))
        has_steps = store.connection.execute(
            "SELECT 1 FROM steps WHERE run_id=?", (run_id,)
        ).fetchone()
        if currency:
            from dreaming.currency import schedule_currency
            from dreaming.sessions import Selection

            selection = Selection((), (), ())
            if not has_steps:
                schedule_currency(store, run_id, config, overrides.get("targets"))
        elif not has_steps and not stats.get("selection_initialized"):
            selection = select_sessions(
                client,
                store,
                config,
                run_id=run_id,
                lenses=roles.lenses,
                scope=SessionScope(**overrides.get("scope", {})),
                **{k: overrides[k] for k in ("since", "until", "reread") if k in overrides},
            )
            store.connection.execute(
                "UPDATE runs SET selection=? WHERE id=?",
                (
                    json.dumps(
                        [
                            {
                                "source": s.document.metadata.source,
                                "session_id": s.document.metadata.id,
                                "start": s.start_entry,
                                "copies": [asdict(m) for m in s.copies],
                                "copy_entry_counts": s.copy_entry_counts,
                                "subject": s.subject,
                                "lenses": [lens.id for lens in s.lenses],
                            }
                            for s in selection.sessions
                        ]
                    ),
                    run_id,
                ),
            )
            store.connection.execute(
                "UPDATE runs SET statistics=json_set(statistics,'$.sessions_skipped',?,"
                "'$.selection_skipped',json(?),'$.selection_diagnostics',json(?),"
                "'$.selection_initialized',json('true')) WHERE id=?",
                (
                    len(selection.skipped),
                    json.dumps(
                        [
                            {
                                "source": s.metadata.source,
                                "session_id": s.metadata.id,
                                "reason": s.reason,
                            }
                            for s in selection.skipped
                        ]
                    ),
                    json.dumps(selection.diagnostics),
                    run_id,
                ),
            )
        else:
            selection = restore_selection(store, config, roles, client, record)
        if not has_steps and not currency:
            schedule_reads(store, run_id, selection)
        backends = {
            name: (
                LMStudioBackend(name, b, run_id)
                if b.harness == "lmstudio"
                else (ClaudeBackend if b.harness == "claude" else CodexBackend)(
                    name, b, store.path.parent, run_id
                )
            )
            for name, b in config.backends.items()
        }
        dispatcher = Dispatcher(
            store, run_id, config, backends, resumed=stats.get("resumed", False)
        )
        from dreaming.adopt import AdoptHandler

        if currency:
            from dreaming.currency import CurrencyAuthorHandler, CurrencyResearchHandler

            handlers = {
                "Research": CurrencyResearchHandler(store, run_id, config, roles),
                "Author": CurrencyAuthorHandler(store, run_id, config, roles),
                "Adopt": AdoptHandler(store, run_id, config, dispatcher=dispatcher, roles=roles),
            }
            return Engine(
                store, run_id, handlers, dispatcher=dispatcher, finalize=finalize
            ).execute()
        handlers = {
            "Read": ReadHandler(store, run_id, config, roles, selection.sessions),
            "Review": PipelineReview(
                store, run_id, config, roles, scope=overrides.get("scope") or None
            ),
            "Research": ResearchHandler(store, run_id, config, roles),
            "Author": AuthorHandler(store, run_id, config, roles),
            "Adopt": AdoptHandler(
                store,
                run_id,
                config,
                dispatcher=dispatcher,
                roles=roles,
                transcript_texts=[
                    entry_text(entry) for s in selection.sessions for entry in s.document.entries
                ],
            ),
        }
        return Engine(store, run_id, handlers, dispatcher=dispatcher, finalize=finalize).execute()
    except Exception as exc:
        from dreaming.redact import redact

        store.connection.execute(
            "UPDATE runs SET status='partial',notice=?,ended_at=? WHERE id=?",
            (redact(str(exc)), datetime.now(UTC).isoformat(), run_id),
        )
        finalize(store, run_id)
        return 5


def restore_selection(store, config, roles, client, record):
    from dreaming.sessions import (
        SelectedSession,
        Selection,
        SessionDocument,
        SessionError,
        SessionMetadata,
        attribute_session,
        parse_document,
    )

    sessions = []
    for ref in json.loads(record["selection"]):
        row = store.connection.execute(
            "SELECT document_path FROM sessions_seen WHERE source=? AND session_id=?",
            (ref["source"], ref["session_id"]),
        ).fetchone()
        doc = (
            parse_document(Path(row[0]).read_text())
            if row and row[0]
            else client.open(ref["session_id"], ref["source"])
        )
        if not isinstance(doc, SessionDocument) or (doc.metadata.source, doc.metadata.id) != (
            ref["source"],
            ref["session_id"],
        ):
            raise SessionError("invalid_document", "Cannot reopen selected session")
        copies = tuple(SessionMetadata(**meta) for meta in ref.get("copies", [])) or (doc.metadata,)
        subject = ref.get("subject")
        attr = attribute_session(doc, store, config, copies=copies, subject=subject)
        lenses = tuple(
            lens
            for lens in roles.lenses
            if lens.id in ref.get("lenses", [item.id for item in roles.lenses])
        )
        sessions.append(
            SelectedSession(
                doc,
                copies,
                ref["start"],
                lenses,
                subject=subject,
                copy_entry_counts=tuple(ref.get("copy_entry_counts", ())),
                attribution=attr,
            )
        )
    return Selection(tuple(sessions), (), ())

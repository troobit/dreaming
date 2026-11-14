"""Verify source evidence before redaction; apply program-verified checkpoints."""

import hashlib
import json
from dataclasses import asdict

from dreaming.config import Config
from dreaming.redact import redact
from dreaming.sessions import SelectedSession, _remember, attribute_session, reader_document
from dreaming.store import Store


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identifier(prefix: str, value) -> str:
    return prefix + ":" + hashlib.sha256(canonical(value).encode()).hexdigest()[:32]


def normalise(text: str) -> str:
    return " ".join(text.split())


def verify_quote(text: str, quote: str) -> bool:
    return bool(normalise(quote)) and normalise(quote) in normalise(text)


def entry_text(entry) -> str:
    return "\n".join(
        text
        for block in entry.content
        if isinstance(
            text := block.data.get("content") if block.type == "tool_result" else block.text, str
        )
    )


def _attribution(entry, quote):
    if any(
        block.type == "tool_result"
        and verify_quote(block.data.get("content", block.text or ""), quote)
        for block in entry.content
    ):
        return "tool_result"
    return {"user": "user_statement", "assistant": "assistant_suggestion"}.get(
        entry.role, "tool_prompt"
    )


def normalise_pattern(key: str, lens_id: str) -> str:
    key = normalise(key).casefold()
    prefix = lens_id.casefold() + ":"
    if key.startswith(prefix):
        key = key[len(prefix) :].strip()
    return prefix + key


def _outcome(observation: dict, entries: dict) -> tuple[str, list[str]]:
    if observation["outcome"] == "unknown":
        return "unknown", []
    for location in observation["outcome_evidence"]:
        entry = entries.get(location)
        if entry is None or entry.supplied_context or entry.unattributed:
            continue
        if entry.role == "user" and not any(b.type == "tool_result" for b in entry.content):
            return (
                "user-confirmed" if observation["outcome"] == "success" else "failed",
                [location, *([entry.id] if entry.id else [])],
            )
        for block in entry.content:
            if block.type != "tool_result":
                continue
            data = block.data
            code = data.get("exit_code")
            failed = data.get("is_error") is True or (type(code) is int and code != 0)
            passed = (type(code) is int and code == 0) or data.get("success") is True
            if observation["outcome"] == "failure" and failed:
                return "failed", [location]
            if observation["outcome"] == "success" and passed and not failed:
                return "check-confirmed", [location]
    return "unknown", []


def verify_observations(
    store: Store,
    config: Config,
    selected: SelectedSession,
    observations: list[dict],
    *,
    backend: str,
    model: str | None,
) -> dict:
    document = reader_document(store, selected, config)
    attr = attribute_session(
        selected.document, store, config, copies=selected.copies, subject=selected.subject
    )
    entries = {f"entry:{entry.source_index}": entry for entry in document.entries}
    lenses = {lens.id: lens for lens in selected.lenses}
    accepted, rejected = [], []
    for observation in observations:
        try:
            location = observation["source_location"]
            entry = entries.get(location)
            if (
                (observation["source"], observation["session_id"])
                != (
                    document.metadata.source,
                    document.metadata.id,
                )
                or type(observation["representation_version"]) is not int
                or observation["representation_version"] != document.schema_version
            ):
                raise ValueError("source_mismatch")
            if entry is None or observation["entry_id"] not in {entry.id, location}:
                raise ValueError("location_mismatch")
            if entry.source_index < selected.start_entry or entry.supplied_context:
                raise ValueError("not_independent_evidence")
            if not verify_quote(entry_text(entry), observation["quote"]):
                raise ValueError("quote_mismatch")
            lens = lenses.get(observation["lens_id"])
            if (
                not lens
                or lens.version != observation["lens_version"]
                or not lens.applies(
                    domain=attr.domain,
                    project=attr.project,
                    workflow=attr.workflow,
                )
            ):
                raise ValueError("lens_mismatch")
            if selected.subject == "dreaming" and observation["subject"] != "dreaming":
                raise ValueError("dream_subject_mismatch")
            kind = _attribution(entry, observation["quote"])
            if (
                observation["kind"] in {"preference", "correction", "user_statement"}
                and kind != "user_statement"
            ):
                raise ValueError("not_user_evidence")
            if observation["modality"] not in {"asserted", "hedged", "tentative"}:
                raise ValueError("invalid_modality")
            outcome, outcome_evidence = _outcome(observation, entries)
            key = [
                document.metadata.source,
                document.metadata.id,
                location,
                document.schema_version,
                lens.id,
                lens.version,
                normalise(observation["quote"]),
                kind,
            ]
            accepted.append(
                dict(
                    id=identifier("e", key),
                    source=document.metadata.source,
                    session_id=document.metadata.id,
                    entry_id=location,
                    source_location=location,
                    source_entry_id=entry.id,
                    representation_version=document.schema_version,
                    redacted_quote=redact(observation["quote"]),
                    kind=kind,
                    observation_type=observation["kind"],
                    modality=observation["modality"],
                    outcome=outcome,
                    outcome_evidence=canonical(outcome_evidence),
                    lens_id=lens.id,
                    lens_version=lens.version,
                    pattern_key=normalise_pattern(observation["pattern_key"], lens.id),
                    backend=backend,
                    model=model,
                    subject=selected.subject or observation["subject"],
                    project=attr.project,
                    domain=attr.domain,
                    observed_at=entry.timestamp or attr.started_at,
                    declared_scope=canonical(observation.get("scope", {})),
                    _source_digest=identifier("source", entry_text(entry)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "invalid_observation"
            rejected.append(reason)
    return {
        "gate_version": 1,
        "accepted": sorted(accepted, key=lambda o: o["id"]),
        "rejected": rejected,
    }


def apply_observations(store, conn, run_id, selected, config, checkpoint) -> None:
    if checkpoint.get("gate_version") != 1:
        raise ValueError("Expected a verified observation checkpoint")
    # Recheck live exclusion and attribution after dispatch and on recovery.
    reader_document(store, selected, config)
    entries = {f"entry:{i}": entry for i, entry in enumerate(selected.document.entries)}
    attr = attribute_session(
        selected.document, store, config, copies=selected.copies, subject=selected.subject
    )
    for meta in selected.copies:
        _remember(conn, meta)
        conn.execute(
            "UPDATE sessions_seen SET project=?,domain=?,workflow=?,interactive=? "
            "WHERE source=? AND session_id=?",
            (attr.project, attr.domain, attr.workflow, int(attr.interactive), meta.source, meta.id),
        )
    inserted = 0
    rejected = list(checkpoint["rejected"])
    lenses = {lens.id: lens for lens in selected.lenses}
    for row in checkpoint["accepted"]:
        row = dict(row)
        digest = row.pop("_source_digest")
        quoted = bool({row["source_location"], row["source_entry_id"]} & attr.user_quote_entries)
        # The original source role was already verified. Registration may have
        # changed it since, even when the original source is no longer present.
        original = entries.get(row["source_location"])
        row["availability"] = (
            "unavailable"
            if original is None
            else "available"
            if identifier("source", entry_text(original)) == digest
            else "changed"
        )
        if row["kind"] == "user_statement" and not attr.interactive and not quoted:
            rejected.append("attribution_changed")
            continue
        lens = lenses[row["lens_id"]]
        scope = {
            key: value
            for key, value in asdict(lens).items()
            if key in {"domains", "projects", "workflows"}
        }
        if selected.subject == "dreaming":
            scope["subject"] = "dreaming"
        scope_json = canonical(scope)
        pid = identifier("p", [row["pattern_key"], scope])
        conn.execute(
            "INSERT OR IGNORE INTO patterns(id,key,lens_scope,created_at) VALUES (?,?,?,?)",
            (pid, row["pattern_key"], scope_json, row["observed_at"]),
        )
        pid = conn.execute(
            "SELECT id FROM patterns WHERE key=? AND lens_scope=?", (row["pattern_key"], scope_json)
        ).fetchone()[0]
        while merged := conn.execute(
            "SELECT merged_into FROM patterns WHERE id=?", (pid,)
        ).fetchone()[0]:
            pid = merged
        row.update(run_id=run_id, pattern_id=pid, project=attr.project, domain=attr.domain)
        columns = list(row)
        inserted += conn.execute(
            f"INSERT OR IGNORE INTO observations({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(row.values()),
        ).rowcount
    stats = json.loads(
        conn.execute("SELECT statistics FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    )
    for key, count in (
        ("observations_accepted", inserted),
        ("observations_rejected", len(rejected)),
        ("quote_mismatches", sum("mismatch" in r for r in rejected)),
    ):
        stats[key] = stats.get(key, 0) + count
    conn.execute("UPDATE runs SET statistics=? WHERE id=?", (canonical(stats), run_id))

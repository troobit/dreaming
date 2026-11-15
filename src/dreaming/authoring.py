"""Research findings and evidence-checked author proposals, without repository writes."""

import json
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

from dreaming.config import ConfigError, git, instruction_revision
from dreaming.knowledge import (
    _rows,
    _support_sufficient,
    available_file_findings,
    available_observations,
    in_scope,
    normalise_scope,
    promotion_status,
)
from dreaming.observe import canonical, entry_text, identifier, verify_quote
from dreaming.redact import redact
from dreaming.sessions import ApsisClient, SessionDocument, SessionError, parse_document
from dreaming.steps import StepDeferred

REMOTE_TOOLS = {"web_search", "web_fetch", "pr_read"}


def change_id(run_id, author_key):
    return identifier("change", [run_id, author_key])


def linked_groups(store, run_id):
    """Undirected dependency components for later group adoption."""
    graph = {}
    for cid, dependencies in store.connection.execute(
        "SELECT id,depends_on FROM changes WHERE run_id=? ORDER BY id", (run_id,)
    ):
        graph.setdefault(cid, set())
        for other in json.loads(dependencies):
            graph[cid].add(other)
            graph.setdefault(other, set()).add(cid)
    groups = []
    while graph:
        pending, group = [min(graph)], set()
        while pending:
            cid = pending.pop()
            if cid not in group:
                group.add(cid)
                pending.extend(graph.pop(cid, ()))
        groups.append(tuple(sorted(group)))
    return sorted(groups)


def _evidence(store, ids):
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        raise StepDeferred("evidence_required")
    rows = available_observations(store, evidence_ids=ids)
    files = available_file_findings(store, ids)
    if {r["id"] for r in [*rows, *files]} != set(ids):
        raise StepDeferred("unavailable_or_forgotten_evidence")
    if any(r["availability"] != "available" or r["imported"] for r in [*rows, *files]):
        raise StepDeferred("unavailable_changed_or_imported_evidence")
    return rows, files


def _sessions(rows):
    return {(r["source"], r["session_id"]) for r in rows}


def _record(conn, run_id, step, disposition, reason):
    conn.execute(
        "INSERT OR IGNORE INTO review_decisions"
        "(run_id,operation_key,operation,disposition,reason) VALUES (?,?,?,?,?)",
        (run_id, step.key, redact(step.input_ref or "{}"), disposition, redact(reason)),
    )


class _Handler:
    def __init__(self, store, run_id, config, roles):
        self.store, self.run_id, self.config, self.roles = store, run_id, config, roles
        self.context_limit = None

    def resize(self, context_limit):
        self.context_limit = context_limit

    def _request(self, **data):
        limits = (
            {name: self.context_limit for name in self.config.roles[self.role]}
            if self.context_limit is not None
            else None
        )
        try:
            return self.roles.build(self.role, context_limits=limits, **data)
        except ValueError as exc:
            raise StepDeferred(str(exc)) from exc

    def failed(self, conn, step, reason):
        _record(conn, self.run_id, step, "deferred", reason)


class ResearchHandler(_Handler):
    role, phase = "researcher", "Research"

    def prepare(self, step):
        candidate = json.loads(step.input_ref)
        rows, files = _evidence(self.store, candidate["evidence_ids"])
        sources = {
            "evidence": [{k: row[k] for k in ("id", "redacted_quote", "modality")} for row in rows]
            + files,
            "repositories": self.config.repositories,
            "knowledge_folders": [],
            "unavailable_sources": [],
        }
        for folder in self.config.knowledge_folders:
            key = "knowledge_folders" if Path(folder).is_dir() else "unavailable_sources"
            sources[key].append(
                folder
                if key == "knowledge_folders"
                else {"source": folder, "reason": "missing_folder"}
            )
        request = self._request(
            question=candidate["question"], scope=candidate.get("scope", {}), sources=sources
        )

        def checkpoint(response, backend):
            data = json.loads(response.raw_output)
            data["unavailable_sources"].extend(sources["unavailable_sources"])
            data.update(
                _backend=backend,
                _model=response.model,
                _external_input=bool(REMOTE_TOOLS & set(request.tools)),
            )
            return canonical(data)

        return {
            **request.metadata(),
            "sessions": tuple(sorted(_sessions(rows))),
            "checkpoint": checkpoint,
        }

    def apply(self, conn, step, raw):
        data = json.loads(raw)
        if "deferred" in data:
            _record(conn, self.run_id, step, "deferred", data["deferred"])
            return ()
        candidate = json.loads(step.input_ref)
        try:
            _evidence(self.store, candidate["evidence_ids"])
        except StepDeferred as exc:
            _record(conn, self.run_id, step, "deferred", str(exc))
            return ()
        for finding in data["findings"]:
            sources = finding["sources"]
            fid = identifier("research-finding", [self.run_id, step.key, finding])
            conn.execute(
                "INSERT OR IGNORE INTO research_findings"
                "(id,run_id,question,finding,sources,backend,model,task_key,"
                "evidence_ids,external_input) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fid,
                    self.run_id,
                    redact(candidate["question"]),
                    redact(finding["finding"]),
                    redact(canonical(sources)),
                    data.get("_backend"),
                    data.get("_model"),
                    step.key,
                    canonical(candidate["evidence_ids"]),
                    int(data.get("_external_input", False) or any("url" in s for s in sources)),
                ),
            )
        _record(conn, self.run_id, step, "applied", "research_retained")
        return ()


class AuthorHandler(_Handler):
    role, phase = "author", "Author"

    def __init__(self, store, run_id, config, roles, *, client=None):
        super().__init__(store, run_id, config, roles)
        self.client = client or ApsisClient(codex_exec_dirs=(store.path.parent / "codex-exec",))

    def _repository(self, name):
        settings = next(
            (r for r in self.config.repositories.values() if r.get("path") == name), None
        )
        if settings is None:
            raise StepDeferred("unconfigured_repository")
        repo = Path(name).resolve()
        _, revision = instruction_revision(
            replace(
                self.config, instruction_repo=repo, default_branch=settings.get("default_branch")
            )
        )
        return repo, revision

    def _file(self, repo, revision, path):
        relative = PurePosixPath(path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or ".git" in relative.parts
        ):
            raise StepDeferred("invalid_edit_path")
        # Read blobs only: a symlink cannot redirect evidence outside the repository.
        mode = git(repo, "ls-tree", revision, "--", path).split(" ", 1)[0]
        if mode not in {"100644", "100755"}:
            raise StepDeferred(f"unavailable_file:{path}")
        return git(repo, "show", f"{revision}:{relative.as_posix()}", strip=False)

    def _verify_sessions(self, rows):
        documents = {}
        for row in rows:
            key = (row["source"], row["session_id"])
            if key not in documents:
                path = self.store.connection.execute(
                    "SELECT document_path FROM sessions_seen WHERE source=? AND session_id=?", key
                ).fetchone()[0]
                doc = (
                    parse_document(Path(path).read_text())
                    if path
                    else self.client.open(key[1], key[0])
                )
                if (
                    not isinstance(doc, SessionDocument)
                    or (doc.metadata.source, doc.metadata.id) != key
                ):
                    raise StepDeferred("source_identity_changed")
                documents[key] = doc
            doc = documents[key]
            location = re.fullmatch(r"entry:(\d+)", row["source_location"])
            index = int(location[1]) if location else len(doc.entries)
            if (
                row["representation_version"] != doc.schema_version
                or index >= len(doc.entries)
                or not verify_quote(redact(entry_text(doc.entries[index])), row["redacted_quote"])
            ):
                self.store.connection.execute(
                    "UPDATE observations SET availability='changed' WHERE id=?", (row["id"],)
                )
                raise StepDeferred(f"changed_source_evidence:{row['id']}")
            entry = doc.entries[index]
            if (
                entry.supplied_context
                or entry.unattributed
                or (row["source_entry_id"] is not None and entry.id != row["source_entry_id"])
                or (row["kind"] == "user_statement" and entry.role != "user")
                or (row["kind"] == "assistant_suggestion" and entry.role != "assistant")
            ):
                raise StepDeferred(f"changed_source_attribution:{row['id']}")

    def _context(self, step):
        candidate = json.loads(step.input_ref)
        skill = candidate.get("skill")
        if skill and skill.get("interactive"):
            raise StepDeferred(f"interactive_skill:{skill['name']}")
        rows, files = _evidence(self.store, candidate["evidence_ids"])
        self._verify_sessions(rows)
        if not files and not _support_sufficient(
            self.store, candidate["evidence_ids"], self.config
        ):
            raise StepDeferred("insufficient_current_support")
        if candidate.get("pattern_id") and any(
            r["pattern_id"] != candidate["pattern_id"] for r in [*rows, *files]
        ):
            raise StepDeferred("pattern_evidence_mismatch")
        current, instructions, repo_info = [], [], None
        repo_name = candidate.get("repo")
        if repo_name:
            repo_info = self._repository(repo_name)
            repo, revision = repo_info
            for path in ("CLAUDE.md", "AGENTS.md"):
                if git(repo, "ls-tree", revision, "--", path):
                    instructions.append(
                        dict(path=path, revision=revision, text=self._file(repo, revision, path))
                    )
        for row in files:
            repo, revision = self._repository(row["repo"])
            text = self._file(repo, revision, row["path"])
            if revision != row["revision"]:
                self.store.connection.execute(
                    "UPDATE file_findings SET availability='changed' WHERE id=?", (row["id"],)
                )
                raise StepDeferred(f"changed_file_revision:{row['id']}")
            location = re.fullmatch(r"line:(\d+)(?:-(\d+))?", row["location"])
            if row["location"] != "file" and (
                not location
                or int(location[1]) < 1
                or int(location[2] or location[1]) > len(text.splitlines())
                or int(location[2] or location[1]) < int(location[1])
            ):
                raise StepDeferred(f"unresolved_file_location:{row['id']}")
            current.append(dict(repo=str(repo), revision=revision, path=row["path"], text=text))
        evidence = [
            {
                k: row[k]
                for k in (
                    "id",
                    "redacted_quote",
                    "modality",
                    "kind",
                    "source",
                    "session_id",
                    "source_location",
                    "observation_type",
                )
            }
            for row in rows
        ] + files
        research = _rows(
            self.store.connection.execute(
                "SELECT * FROM research_findings WHERE run_id=? ORDER BY id", (self.run_id,)
            )
        )
        research = [r for r in research if r["task_key"] in step.depends_on]
        sessions = _sessions(rows)
        for finding in research:
            source_rows, _ = _evidence(
                self.store, json.loads(finding["evidence_ids"]) or candidate["evidence_ids"]
            )
            sessions |= _sessions(source_rows)
            finding["sources"] = json.loads(finding["sources"])
        enriched = candidate | {
            "evidence": evidence,
            "change_id": change_id(self.run_id, step.key),
            "other_changes": [
                {"id": change_id(self.run_id, key), "repo": json.loads(ref).get("repo")}
                for key, ref in self.store.connection.execute(
                    "SELECT key,input_ref FROM steps WHERE run_id=? "
                    "AND phase='Author' AND key<>? ORDER BY key",
                    (self.run_id, step.key),
                )
            ],
        }
        return (
            dict(
                candidate=enriched,
                research=research,
                current_files=current,
                target_instructions=instructions,
            ),
            rows,
            files,
            sessions,
            repo_info,
        )

    def prepare(self, step):
        try:
            data, _, _, sessions, _ = self._context(step)
        except (ConfigError, SessionError, OSError, ValueError) as exc:
            raise StepDeferred(f"evidence_unavailable:{exc}") from exc
        request = self._request(**data)
        # This phase produces deltas; repository mutation belongs to Adopt.
        request = replace(request, tools=[t for t in request.tools if t == "Read"])
        external = any(r["external_input"] for r in data["research"])

        def checkpoint(response, backend):
            return canonical(
                json.loads(response.raw_output)
                | {
                    "_backend": backend,
                    "_model": response.model,
                    "_external_input": external,
                }
            )

        return {**request.metadata(), "sessions": tuple(sorted(sessions)), "checkpoint": checkpoint}

    def _dependencies(self, step, dependencies):
        own = change_id(self.run_id, step.key)
        known = {
            cid
            for (cid,) in self.store.connection.execute(
                "SELECT id FROM changes WHERE run_id=?", (self.run_id,)
            )
        }
        known |= {
            change_id(self.run_id, key)
            for (key,) in self.store.connection.execute(
                "SELECT key FROM steps WHERE run_id=? AND phase='Author'", (self.run_id,)
            )
        }
        if own in dependencies or set(dependencies) - known:
            raise StepDeferred("invalid_change_dependency")
        graph = {
            cid: json.loads(ref)
            for cid, ref in self.store.connection.execute(
                "SELECT id,depends_on FROM changes WHERE run_id=?", (self.run_id,)
            )
        }
        graph[own] = dependencies

        def visit(cid, parents):
            if cid in parents:
                raise StepDeferred("change_dependency_cycle")
            for other in graph.get(cid, []):
                visit(other, parents | {cid})

        visit(own, set())

    def before_apply(self, step, raw):
        data = json.loads(raw)
        try:
            context, rows, files, _, repo_info = self._context(step)
            ids = set(context["candidate"]["evidence_ids"])
            cited = set(data["evidence_ids"])
            if not cited or cited - ids:
                raise StepDeferred("evidence_not_in_author_input")
            skill = data.get("skill")
            if skill and skill["interactive"]:
                raise StepDeferred(f"interactive_skill:{skill['name']}")
            if skill and skill["name"] not in self.config.tools["author"]:
                raise StepDeferred(f"unavailable_skill:{skill['name']}")
            if data["disposition"] == "defer":
                raise StepDeferred(data["reason"])
            if data["disposition"] == "learning":
                learning = data["learning"]
                if not learning or data["edits"] or data["depends_on"]:
                    raise StepDeferred("invalid_learning_output")
                support = set(learning["evidence_ids"])
                scope = normalise_scope(learning["scope"])
                matched = [r for r in rows if r["id"] in support and in_scope(r, scope)]
                if (
                    scope["unresolved"]
                    or not support
                    or support - cited
                    or len(matched) != len(support)
                ):
                    raise StepDeferred("invalid_learning_evidence_or_scope")
                if scope["global"] and not any(
                    json.loads(r["declared_scope"]).get("global") for r in matched
                ):
                    raise StepDeferred("unsupported_learning_scope")
                if (
                    promotion_status(matched, self.config.thresholds.min_sessions, "learning")
                    != "active"
                ):
                    raise StepDeferred("insufficient_user_learning_evidence")
                data["_scope"] = scope
            else:
                if not repo_info or not data["edits"] or data["learning"]:
                    raise StepDeferred("repository_and_delta_edits_required")
                repo, revision = repo_info
                seen, texts = set(), {}
                for edit in data["edits"]:
                    if edit["id"] in seen:
                        raise StepDeferred("duplicate_edit_id")
                    seen.add(edit["id"])
                    supporting, counter = (
                        set(edit["supporting_evidence"]),
                        set(edit["counter_evidence"]),
                    )
                    if (
                        not supporting
                        or not counter
                        or supporting & counter
                        or (supporting | counter) - cited
                    ):
                        raise StepDeferred("supporting_and_counter_cases_required")
                    path = edit["path"]
                    text = texts.get(path)
                    if text is None:
                        text = self._file(repo, revision, path)
                    before = edit["before"]
                    if (
                        not before.strip()
                        or text.strip() == before.strip()
                        or text.count(before) != 1
                        or edit["after"] == before
                    ):
                        raise StepDeferred("edit_requires_unique_partial_preimage")
                    texts[path] = text.replace(before, edit["after"], 1)
                self._dependencies(step, data["depends_on"])
                data["_revision"] = revision
            data["_external_input"] = bool(
                data.get("_external_input")
                or data["external_input"]
                or any(r["external_input"] for r in context["research"])
            )
        except (StepDeferred, ConfigError, SessionError, OSError, ValueError) as exc:
            data = {"deferred": str(exc)}
        return redact(canonical(data))

    def apply(self, conn, step, raw):
        data = json.loads(raw)
        if "deferred" in data:
            _record(conn, self.run_id, step, "deferred", data["deferred"])
            return ()
        candidate = json.loads(step.input_ref)
        try:
            _evidence(self.store, candidate["evidence_ids"])
        except StepDeferred as exc:
            _record(conn, self.run_id, step, "deferred", str(exc))
            return ()
        if data["disposition"] == "learning":
            recommendation = data["learning"]
            lid = identifier("learning", [candidate.get("pattern_id"), data["_scope"]])
            conn.execute(
                "INSERT OR IGNORE INTO learning(id,run_id,scope,evidence,next_step,outcome) "
                "VALUES (?,?,?,?,?,?)",
                (
                    lid,
                    self.run_id,
                    canonical(data["_scope"]),
                    canonical(recommendation["evidence_ids"]),
                    recommendation["next_step"],
                    recommendation["outcome"],
                ),
            )
            reason = "learning_recommendation_retained"
        else:
            conn.execute(
                "INSERT OR IGNORE INTO changes(id,run_id,pattern_id,repo,external_input,"
                "rank_signals,depends_on,evidence,reason,authoring_id,edits,backend,model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id(self.run_id, step.key),
                    self.run_id,
                    candidate.get("pattern_id"),
                    candidate["repo"],
                    int(data["_external_input"]),
                    canonical(candidate.get("rank_signals", {})),
                    canonical(sorted(set(data["depends_on"]))),
                    canonical(sorted(set(data["evidence_ids"]))),
                    data["reason"],
                    step.key,
                    canonical(data["edits"]),
                    data.get("_backend"),
                    data.get("_model"),
                ),
            )
            reason = "repository_change_authored"
        _record(conn, self.run_id, step, "applied", reason)
        return ()

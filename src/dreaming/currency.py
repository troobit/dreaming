"""Currency-review dreams: research per target, verify cited passages, author on a branch.

A currency run reads no sessions. Findings stay in `currency_findings`, apart from
observations, and never count as support for knowledge about the user.
"""

import html.parser
import ipaddress
import json
import socket
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dreaming.authoring import AuthorHandler, _Handler, _record
from dreaming.config import ConfigError, instruction_revision
from dreaming.observe import canonical, identifier, normalise
from dreaming.redact import redact
from dreaming.steps import Step, StepDeferred, insert_steps

WEB_TOOLS = {"Read", "web_search", "web_fetch"}
MAX_PAGE_CHARS = 200_000
MULTI_PART_SUFFIXES = {
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.jp",
    "co.nz",
    "com.br",
    "co.in",
}
SUPPRESSED = {"dismissed", "branch_deleted"}


def registrable_domain(url: str) -> str:
    """Approximate registrable domain without a public-suffix list."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    keep = 3 if ".".join(labels[-2:]) in MULTI_PART_SUFFIXES else 2
    return ".".join(labels[-keep:])


def passage_in(text: str, passage: str) -> bool:
    """Verbatim check, ignoring only differences in whitespace."""
    needle = normalise(passage)
    return bool(needle) and needle in normalise(text)


def independent_sources(sources: list[dict]) -> list[dict]:
    """Keep sources on distinct registrable domains whose passages do not overlap."""
    kept, domains = [], set()
    for source in sources:
        domain, passage = registrable_domain(source["url"]), normalise(source["passage"])
        if domain in domains or any(
            passage in (other := normalise(k["passage"])) or other in passage for k in kept
        ):
            continue
        domains.add(domain)
        kept.append(source)
    return kept


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        self._skip += tag in {"script", "style"}
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self._skip -= tag in {"script", "style"} and self._skip > 0
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def fetch_page(url: str, *, timeout: float = 15) -> tuple[str, str]:
    """Fetch a public http(s) page; returns (title, text). Raises OSError when unusable."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise OSError("unsupported_url")
    for info in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise OSError("non_public_address")
    request = urllib.request.Request(url, headers={"User-Agent": "dreaming-currency/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read(MAX_PAGE_CHARS * 4)
    parser = _Text()
    parser.feed(body.decode("utf-8", errors="replace"))
    return normalise(parser.title), normalise(" ".join(parser.parts))[:MAX_PAGE_CHARS]


def schedule_currency(store, run_id, config, names=None):
    """Research steps only: no Read phase and no session selection."""
    targets = [t for t in config.currency_targets if names is None or t["name"] in names]
    if not targets:
        raise ConfigError("No currency targets configured")
    with store.transaction() as conn:
        insert_steps(
            conn,
            run_id,
            tuple(
                Step(
                    "currency-research:" + t["name"],
                    "Research",
                    input_ref=canonical({"currency": True, "target": t}),
                )
                for t in targets
            ),
        )


def dismiss_finding(store, finding_id):
    """Dismissed findings are not proposed again without a new independent source."""
    store.connection.execute(
        "UPDATE currency_findings SET status='dismissed' WHERE id=?", (finding_id,)
    )


def _default_revision(config, repo, repo_settings):
    return instruction_revision(
        replace(
            config, instruction_repo=Path(repo), default_branch=repo_settings.get("default_branch")
        )
    )[1]


def _adopt_steps(conn, run_id, current, new_keys=()):
    """Adopt waits for every Research and Author step, once the last Research step is done."""
    if conn.execute("SELECT 1 FROM steps WHERE run_id=? AND phase='Adopt'", (run_id,)).fetchone():
        return ()
    if conn.execute(
        "SELECT 1 FROM steps WHERE run_id=? AND phase='Research' AND key<>? "
        "AND state IN ('pending','running')",
        (run_id, current),
    ).fetchone():
        return ()
    keys = [
        k
        for (k,) in conn.execute(
            "SELECT key FROM steps WHERE run_id=? AND phase IN ('Research','Author') AND key<>?",
            (run_id, current),
        )
    ]
    return (Step("adopt", "Adopt", tuple(dict.fromkeys([*keys, current, *new_keys]))),)


class CurrencyResearchHandler(_Handler):
    role, phase = "researcher", "Research"

    def __init__(self, store, run_id, config, roles, *, fetch=fetch_page, now=None):
        super().__init__(store, run_id, config, roles)
        self.fetch = fetch
        self.now = now or (lambda: datetime.now(UTC).strftime("%Y-%m-%d"))

    def prepare(self, step):
        target = json.loads(step.input_ref)["target"]
        request = self._request(
            question=(
                f"Is the {target['kind']} target '{target['name']}' ({', '.join(target['paths'])}) "
                "current? Find current literature and implementations of the practices it "
                "embeds, and report where it is outdated, contradicted or lacks a documented "
                "practice. Cite at most "
                f"{self.config.thresholds.max_sources} web pages in total."
            ),
            scope={"target": target["name"]},
            sources={
                "target": target,
                "max_sources": self.config.thresholds.max_sources,
                "knowledge_folders": [f for f in self.config.knowledge_folders if Path(f).is_dir()],
                "unavailable_sources": [],
            },
        )
        request = replace(request, tools=[t for t in request.tools if t in WEB_TOOLS])

        def checkpoint(response, backend):
            data = json.loads(response.raw_output)
            data.update(
                _backend=backend, _model=response.model, _pages=self._pages(data["findings"])
            )
            return canonical(data)

        return {**request.metadata(), "sessions": (), "checkpoint": checkpoint}

    def _pages(self, findings):
        known = {
            url: status
            for url, status in self.store.connection.execute(
                "SELECT url,status FROM currency_pages WHERE run_id=?", (self.run_id,)
            )
        }
        used, pages = len(known), {}
        urls = dict.fromkeys(
            s["url"]
            for f in findings
            for s in f["sources"]
            if s.get("url") and s["url"] not in known
        )
        for url in urls:
            if used >= self.config.thresholds.max_sources:
                pages[url] = dict(status="unavailable", reason="source_limit")
                continue
            used += 1
            try:
                title, text = self.fetch(url)
                pages[url] = dict(
                    status="fetched",
                    title=redact(title),
                    text=redact(text),
                    retrieved_at=self.now(),
                )
            except (OSError, ValueError) as exc:
                pages[url] = dict(status="unavailable", reason=redact(str(exc)) or "fetch_failed")
        return pages

    def _decide(self, conn, step, index, disposition, reason, item_id=None):
        conn.execute(
            "INSERT OR IGNORE INTO review_decisions"
            "(run_id,operation_key,operation,disposition,reason,item_id) VALUES (?,?,?,?,?,?)",
            (
                self.run_id,
                f"{step.key}:{index}",
                redact(step.input_ref or "{}"),
                disposition,
                reason,
                item_id,
            ),
        )

    def failed(self, conn, step, reason):
        _record(conn, self.run_id, step, "deferred", reason)
        insert_steps(conn, self.run_id, _adopt_steps(conn, self.run_id, step.key))

    def _branch_deleted(self, conn, finding_id):
        """A committed change whose branch is gone without being merged."""
        from dreaming.adopt import _git_ok

        for repo, branch, commit in conn.execute(
            "SELECT repo,branch,commit_sha FROM changes "
            "WHERE currency_finding_id=? AND branch IS NOT NULL AND commit_sha IS NOT NULL",
            (finding_id,),
        ):
            settings = next(
                (r for r in self.config.repositories.values() if r.get("path") == repo), {}
            )
            try:
                head = _default_revision(self.config, repo, settings)
            except ConfigError:
                continue
            if _git_ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
                continue
            if _git_ok(repo, "merge-base", "--is-ancestor", commit, head):
                continue
            return True
        return False

    def _verified(self, finding, pages):
        """Web sources whose passages appear in stored text, or the reason to reject."""
        verified = []
        for source in finding["sources"]:
            url = source.get("url")
            page = pages.get(url)
            if not url or not page or page["status"] != "fetched":
                continue
            if not passage_in(page["text"], source["passage"]):
                return None, f"passage_not_in_stored_text:{url}"
            verified.append(
                dict(
                    id=identifier("currency-source", url),
                    title=page["title"] or source["title"],
                    url=url,
                    retrieved_at=page["retrieved_at"],
                    claim=redact(source["claim"]),
                    passage=redact(source["passage"]),
                )
            )
        return (verified, None) if verified else (None, "no_fetched_source")

    def _finding(self, conn, step, index, target, finding, pages):
        """Returns the finding id when it becomes an authoring candidate."""
        verified, reason = self._verified(finding, pages)
        if reason:
            self._decide(conn, step, index, "rejected", reason)
            return None
        independent = independent_sources(verified)
        claim = redact(finding["finding"])
        fid = identifier("currency", [target["name"], normalise(claim).casefold()])
        row = conn.execute(
            "SELECT status,sources FROM currency_findings WHERE id=?", (fid,)
        ).fetchone()
        merged = verified
        if row:
            old = json.loads(row[1])
            merged = verified + [s for s in old if s["url"] not in {v["url"] for v in verified}]
            if row[0] in SUPPRESSED or self._branch_deleted(conn, fid):
                known = {registrable_domain(s["url"]) for s in old}
                if all(registrable_domain(s["url"]) in known for s in independent):
                    self._decide(conn, step, index, "deferred", "no_new_independent_source", fid)
                    return None
            elif row[0] == "proposed":
                conn.execute(
                    "UPDATE currency_findings SET sources=? WHERE id=?",
                    (canonical(merged), fid),
                )
                self._decide(conn, step, index, "applied", "equivalent_finding_reused", fid)
                return None
        candidate = len(independent) >= self.config.thresholds.currency_min_sources
        status = "candidate" if candidate else "note"
        conn.execute(
            "INSERT INTO currency_findings(id,run_id,target,claim,sources,status) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id,"
            "sources=excluded.sources,status=excluded.status",
            (fid, self.run_id, target["name"], claim, canonical(merged), status),
        )
        self._decide(
            conn,
            step,
            index,
            "applied",
            "candidate" if candidate else "retained_as_note:too_few_independent_sources",
            fid,
        )
        return fid if candidate else None

    def apply(self, conn, step, raw):
        data = json.loads(raw)
        if "deferred" in data:
            self.failed(conn, step, data["deferred"])
            return ()
        target = json.loads(step.input_ref)["target"]
        for url, page in data.get("_pages", {}).items():
            conn.execute(
                "INSERT OR IGNORE INTO currency_pages"
                "(run_id,url,title,retrieved_at,text,status,reason) VALUES (?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    url,
                    page.get("title"),
                    page.get("retrieved_at"),
                    page.get("text"),
                    page["status"],
                    page.get("reason"),
                ),
            )
        pages = {
            url: dict(status=status, title=title, text=text, retrieved_at=at)
            for url, status, title, text, at in conn.execute(
                "SELECT url,status,title,text,retrieved_at FROM currency_pages WHERE run_id=?",
                (self.run_id,),
            )
        }
        authors = []
        for index, finding in enumerate(data["findings"]):
            fid = self._finding(conn, step, index, target, finding, pages)
            if fid and fid not in {a.key for a in authors}:
                authors.append(
                    Step(
                        fid,
                        "Author",
                        (step.key,),
                        canonical({"currency_finding_id": fid, "target": target}),
                    )
                )
        conn.execute(
            "INSERT OR IGNORE INTO review_decisions"
            "(run_id,operation_key,operation,disposition,reason) VALUES (?,?,?,?,?)",
            (
                self.run_id,
                step.key,
                redact(step.input_ref),
                "applied",
                "currency_research_retained",
            ),
        )
        return (*authors, *_adopt_steps(conn, self.run_id, step.key, [a.key for a in authors]))


class CurrencyAuthorHandler(AuthorHandler):
    """Authors one currency finding. Output is committed to the branch, never merged."""

    def __init__(self, store, run_id, config, roles):
        super().__init__(store, run_id, config, roles, client=object())

    def _finding(self, step):
        fid = json.loads(step.input_ref)["currency_finding_id"]
        row = self.store.connection.execute(
            "SELECT claim,sources,status FROM currency_findings WHERE id=?", (fid,)
        ).fetchone()
        if row is None or row[2] != "candidate":
            raise StepDeferred("finding_not_a_candidate")
        return fid, row[0], json.loads(row[1])

    def prepare(self, step):
        fid, claim, sources = self._finding(step)
        target = json.loads(step.input_ref)["target"]
        try:
            repo, revision = self._repository(target["repo"])
            files = [
                dict(
                    repo=str(repo),
                    revision=revision,
                    path=path,
                    text=self._file(repo, revision, path),
                )
                for path in target["paths"]
            ]
        except (ConfigError, OSError) as exc:
            raise StepDeferred(f"target_unavailable:{exc}") from exc
        texts = {
            url: text
            for url, text in self.store.connection.execute(
                "SELECT url,text FROM currency_pages WHERE run_id=? AND status='fetched'",
                (self.run_id,),
            )
        }
        request = self._request(
            candidate=dict(
                op="currency_change",
                currency_finding_id=fid,
                summary=claim,
                repo=target["repo"],
                target={k: target[k] for k in ("name", "kind", "paths")},
                evidence_ids=[s["id"] for s in sources],
                change_id=self._change_id(step),
            ),
            # Fetched text is untrusted data, carried only in these delimited fields.
            research=[
                dict(
                    finding=claim,
                    external_input=True,
                    sources=[{**s, "fetched_text": texts.get(s["url"], "")} for s in sources],
                )
            ],
            current_files=files,
            target_instructions=[],
        )
        request = replace(request, tools=[t for t in request.tools if t == "Read"])
        return {**request.metadata(), "sessions": (), "checkpoint": self._checkpoint}

    def _change_id(self, step):
        from dreaming.authoring import change_id

        return change_id(self.run_id, step.key)

    @staticmethod
    def _checkpoint(response, backend):
        return canonical(
            json.loads(response.raw_output)
            | {"_backend": backend, "_model": response.model, "_external_input": True}
        )

    def before_apply(self, step, raw):
        data = json.loads(raw)
        try:
            _, _, sources = self._finding(step)
            ids = {s["id"] for s in sources}
            target = json.loads(step.input_ref)["target"]
            if data["disposition"] != "change":
                # Not expressible as a repository change: kept as a currency note.
                data["_note"] = data["reason"]
            else:
                cited = set(data["evidence_ids"])
                if not cited or cited - ids or not data["edits"]:
                    raise StepDeferred("evidence_not_in_author_input")
                repo, revision = self._repository(target["repo"])
                seen, texts = set(), {}
                for edit in data["edits"]:
                    support = set(edit["supporting_evidence"])
                    if edit["id"] in seen or not support or support - cited:
                        raise StepDeferred("supporting_sources_required")
                    seen.add(edit["id"])
                    if edit["path"] not in target["paths"]:
                        raise StepDeferred("edit_outside_target")
                    text = texts.get(edit["path"]) or self._file(repo, revision, edit["path"])
                    before = edit["before"]
                    if (
                        not before.strip()
                        or text.strip() == before.strip()
                        or text.count(before) != 1
                        or edit["after"] == before
                    ):
                        raise StepDeferred("edit_requires_unique_partial_preimage")
                    texts[edit["path"]] = text.replace(before, edit["after"], 1)
                data["_revision"] = revision
        except (StepDeferred, ConfigError, OSError, ValueError) as exc:
            data = {"deferred": str(exc)}
        return redact(canonical(data))

    def apply(self, conn, step, raw):
        data = json.loads(raw)
        fid = json.loads(step.input_ref)["currency_finding_id"]
        if "deferred" in data:
            _record(conn, self.run_id, step, "deferred", data["deferred"])
            conn.execute("UPDATE currency_findings SET status='note' WHERE id=?", (fid,))
            return ()
        if "_note" in data:
            _record(conn, self.run_id, step, "applied", "retained_as_currency_note")
            conn.execute("UPDATE currency_findings SET status='note' WHERE id=?", (fid,))
            return ()
        target = json.loads(step.input_ref)["target"]
        conn.execute(
            "INSERT OR IGNORE INTO changes(id,run_id,currency_finding_id,repo,external_input,"
            "depends_on,evidence,reason,authoring_id,edits,backend,model) "
            "VALUES (?,?,?,?,1,'[]',?,?,?,?,?,?)",
            (
                self._change_id(step),
                self.run_id,
                fid,
                target["repo"],
                canonical(sorted(set(data["evidence_ids"]))),
                data["reason"],
                step.key,
                canonical(data["edits"]),
                data.get("_backend"),
                data.get("_model"),
            ),
        )
        conn.execute("UPDATE currency_findings SET status='proposed' WHERE id=?", (fid,))
        _record(conn, self.run_id, step, "applied", "currency_change_authored")
        return ()

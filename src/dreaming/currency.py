"""The currency-review dream: configured skills, rules and workflows checked
against current sources (4.6, 15.2, 16.1–16.6, Decision 14).

A currency run reads no sessions. Its planner adds one Research step per
configured target and the Adopt step. The researcher reads the target and
searches the web and the knowledge folders with its own tools. The program then
reads every source a finding cites itself, a web page by fetching it and a note
from its knowledge folder, stores what it read, and checks that each relied-on
passage appears there verbatim. A finding with a passage that is not there is
rejected; a source the program cannot read is listed and skipped (16.2).

Sources count as independent only on distinct registrable domains and when they
share no relied-on passage. A finding that shows the target outdated,
contradicted or missing a practice, on at least `currency_min_sources`
independent sources, is a candidate for the Author step, whose change is
committed to the dream branch and never merged (16.3, `adopt.held`). Any other
finding is kept as a currency note (16.4). Findings are never observations
(16.5).

A finding keeps its identifier across dreams: the researcher may name an
equivalent earlier finding, and the same claim about the same target has the
same identifier. A dismissed finding, or one whose branch was deleted without
being merged, is proposed again only with a source on a domain it did not cite
(16.6).
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from dreaming import adopt, author, config, knowledge, research, review, steps
from dreaming.errors import Rejected
from dreaming.observe import normalise, verify
from dreaming.run import Context, Outcome
from dreaming.steps import NewStep, Step

# Assessments that call for a change to the target (16.3).
CHANGE_ASSESSMENTS = ("outdated", "contradicted", "missing_practice")
WEB_SCHEMES = ("http", "https")
# Notes in the knowledge folders count as one source between them: they are the
# user's own notes, not independent of each other.
FOLDERS = "knowledge folders"
FETCH_TIMEOUT_SECONDS = 30
MAX_PAGE_BYTES = 2_000_000
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/json", "application/xml")


def plan(ctx: Context) -> list[NewStep]:
    """A currency run's steps: one Research step per configured target, then
    adoption, which also waits for the Author steps research adds (16.1)."""
    planned = [
        NewStep(f"research:{knowledge.new_id('t', name)}", "research", input_ref=name)
        for name in dict.fromkeys(ctx.cfg["currency"]["targets"])
    ]
    keys = tuple(step.key for step in planned)
    return [*planned, NewStep(adopt.PLAN_KEY, "adopt", depends_on=keys)]


# Sources (16.2, 16.3)


def registrable_domain(link: str) -> str | None:
    """The site an http or https link belongs to, for counting independent
    sources, or None for any other link. Without the public suffix list this is
    the host's last two labels. That never splits one registrable domain in
    two; at worst, two sites under a suffix such as `co.uk` count once."""
    try:
        parts = urlsplit(link.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in WEB_SCHEMES or not host:
        return None
    host = host.rstrip(".")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return ".".join(host.split(".")[-2:])


def _public(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def independent(sources: Sequence[Mapping]) -> int:
    """How many sources are independent: each on a domain no counted source is
    on, relying on a passage that no earlier source's passage contains or is
    contained in (16.3)."""
    domains: set[str] = set()
    passages: list[str] = []
    count = 0
    for source in sources:
        passage = normalise(source["passage"]).lower()
        shared = any(passage in seen or seen in passage for seen in passages)
        passages.append(passage)
        if not shared and source["domain"] not in domains:
            domains.add(source["domain"])
            count += 1
    return count


# Reading a page

BLOCKS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
}  # fmt: skip
SKIPPED = {"script", "style", "noscript", "template", "svg"}


class _Text(HTMLParser):
    """A page's title and its text as a reader sees it: inline markup joins its
    words, block markup separates them, scripts and styles are left out."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.skipping = 0
        self.in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIPPED:
            self.skipping += 1
        elif tag == "title":
            self.in_title = True
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED:
            self.skipping = max(self.skipping - 1, 0)
        elif tag == "title":
            self.in_title = False
        elif tag in BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            (self.title if self.in_title else self.parts).append(data)


def page_text(html: str) -> tuple[str | None, str]:
    """An HTML page's title and text."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.title).split()) or None, "".join(parser.parts)


@dataclass(frozen=True)
class Page:
    title: str | None
    text: str


class FetchFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def fetch_page(link: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> Page:
    """A web page's title and text, read once by the program (16.3)."""
    request = urllib.request.Request(
        link, headers={"User-Agent": "dreaming currency review"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            kind = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read(MAX_PAGE_BYTES)
    except (OSError, ValueError) as exc:
        raise FetchFailed(str(exc)) from None
    if not kind.startswith(TEXT_TYPES):
        raise FetchFailed(f"the page is {kind}, not text")
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if kind in ("text/html", "application/xhtml+xml"):
        return Page(*page_text(text))
    return Page(None, text)


Fetch = Callable[[str, float], Page]


class _Reader:
    """Reads each cited source once per run: a web page within the run's source
    limit, or a note inside a configured knowledge folder. What it read, or why
    it could not, is a `fetched_pages` row."""

    def __init__(
        self, fetch: Fetch, known: dict, limit: int, folders: list[str], today: str
    ) -> None:
        self.fetch = fetch
        self.known = known
        self.limit = limit
        self.folders = [Path(folder).resolve() for folder in folders]
        self.today = today
        self.fetched = sum(
            1
            for row in known.values()
            if row["text"] is not None and registrable_domain(row["link"])
        )
        self.new: dict[str, dict] = {}

    def remaining(self) -> int:
        return max(self.limit - self.fetched, 0)

    def unavailable(self, link: str, reason: str) -> None:
        if link not in self.known:
            self.known[link] = self.new[link] = _row(link, error=reason)

    def read(self, link: str) -> dict:
        if link not in self.known:
            self.known[link] = self.new[link] = self._read(link)
        return self.known[link]

    def _read(self, link: str) -> dict:
        if registrable_domain(link) is None:
            return self._note(link)
        if not _public(urlsplit(link).hostname.rstrip(".")):
            return _row(link, error="not a public web address")
        if self.fetched >= self.limit:
            return _row(link, error=f"the source limit of {self.limit} was reached")
        try:
            page = self.fetch(link, FETCH_TIMEOUT_SECONDS)
        except FetchFailed as failed:
            return _row(link, error=failed.reason)
        self.fetched += 1
        return _row(link, title=page.title, text=page.text, retrieved_on=self.today)

    def _note(self, link: str) -> dict:
        path = Path(link).expanduser().resolve()
        if not any(path.is_relative_to(folder) for folder in self.folders):
            return _row(
                link,
                error="not an http or https page, and not in a configured "
                "knowledge folder",
            )
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            return _row(link, error=exc.strerror or str(exc))
        return _row(link, text=text, retrieved_on=self.today)


def _row(link: str, **fields) -> dict:
    return {
        "link": link,
        "title": None,
        "retrieved_on": None,
        "text": None,
        "error": None,
        **fields,
    }


# The Research step of a currency run


@dataclass
class Checked:
    """A finding as the researcher gave it, the sources the program read and
    found its passages in, and why it is rejected, if it is."""

    found: dict
    sources: list[dict] = field(default_factory=list)
    problem: str | None = None


@dataclass
class Reviewed:
    cfg: dict
    target: str
    now: str
    missing: str | None = None
    output: dict | None = None
    checked: list[Checked] = field(default_factory=list)
    pages: dict[str, dict] = field(default_factory=dict)
    # Changes of earlier findings whose branch was deleted unmerged, by change
    # id, with the finding each rests on.
    deleted: dict[str, str] = field(default_factory=dict)


class CurrencyPhase:
    """Research steps of a currency run, one per target."""

    def __init__(self, instructions, fetch: Fetch = fetch_page) -> None:
        self.instructions = instructions
        self.fetch = fetch

    def prepare(self, ctx: Context, step: Step) -> Reviewed:
        conn, cfg, target = ctx.conn, ctx.cfg, step.input_ref
        name, path = config.split_target(target)
        reviewed = Reviewed(cfg, target, ctx.runner.now())
        try:
            repo = author.repository(cfg, name)
        except Rejected as rejected:
            reviewed.missing = rejected.reason
            return reviewed
        if not repo.exists(path):
            reviewed.missing = (
                f"{path} does not exist in {name} at {repo.revision[:12]}"
            )
            return reviewed
        reviewed.deleted = _deleted_branches(conn, cfg, target)
        known = {
            row["link"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM fetched_pages WHERE run = ?", (ctx.run_id,)
            )
        }
        repositories, folders, missing = research.readable(cfg)
        limit = cfg["thresholds"]["max_sources"]
        reader = _Reader(self.fetch, known, limit, folders, reviewed.now[:10])
        data = {
            "target": {
                "name": target,
                "repo": name,
                "path": path,
                "revision": repo.revision,
            },
            "repositories": repositories,
            "knowledge_folders": folders,
            "earlier_findings": [
                dict(row)
                for row in conn.execute(
                    "SELECT id, claim, assessment, status FROM currency_findings"
                    " WHERE target = ? ORDER BY id",
                    (target,),
                )
            ],
            "max_sources": reader.remaining(),
        }
        role = self.instructions.roles["researcher"]
        reviewed.output = ctx.call("researcher", role.prompt(data), role.schema)
        for unread in [*missing, *reviewed.output["unavailable"]]:
            reader.unavailable(unread["source"], unread["reason"])
        reviewed.checked = [
            _check(found, reader) for found in reviewed.output["findings"]
        ]
        reviewed.pages = reader.new
        return reviewed

    def apply(self, conn: sqlite3.Connection, step: Step, r: Reviewed) -> Outcome:
        if r.missing is not None:
            return Outcome(reason=r.missing)
        conn.executemany(
            "INSERT OR IGNORE INTO fetched_pages (run, target, link, title,"
            " retrieved_on, text, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    step.run,
                    r.target,
                    page["link"],
                    page["title"],
                    page["retrieved_on"],
                    page["text"],
                    page["error"],
                    r.now,
                )
                for page in r.pages.values()
            ],
        )
        reason = "its branch was deleted without being merged"
        for change_id, finding_id in r.deleted.items():
            conn.execute(
                "UPDATE changes SET status = 'rejected', reason = ? WHERE id = ?",
                (reason, change_id),
            )
            conn.execute(
                "UPDATE currency_findings SET status = 'dismissed', reason = ?,"
                " updated_at = ? WHERE id = ?",
                (reason, r.now, finding_id),
            )
        authors = []
        for checked in r.checked:
            candidate_id = _Finding(conn, step, r, checked).keep()
            if candidate_id is not None:
                authors.append(
                    NewStep(f"author:{candidate_id}", "author", input_ref=candidate_id)
                )
        steps.wait_for(conn, step.run, adopt.PLAN_KEY, [s.key for s in authors])
        return Outcome(follow_up=authors)


def _deleted_branches(conn: sqlite3.Connection, cfg: dict, target: str) -> dict:
    """Unmerged changes for the target's findings whose dream branch is gone and
    whose commit is not in the default branch: the user deleted them (16.6)."""
    rows = conn.execute(
        "SELECT c.id, c.repo, c.branch, c.commit_sha, c.currency_finding_id"
        " FROM changes c JOIN currency_findings f ON f.id = c.currency_finding_id"
        " WHERE f.target = ? AND f.status = 'proposed'"
        f" AND c.status IN {review.UNMERGED} AND c.branch IS NOT NULL ORDER BY c.id",
        (target,),
    )
    deleted = {}
    for row in rows:
        entry = cfg["repositories"].get(row["repo"])
        path = adopt.repo_path(cfg, row["repo"]) if entry else None
        if (
            path is None
            or not path.is_dir()
            or adopt.branch_exists(path, row["branch"])
        ):
            continue
        merged = row["commit_sha"] and adopt.is_ancestor(
            path, row["commit_sha"], entry["default_branch"]
        )
        if not merged:
            deleted[row["id"]] = row["currency_finding_id"]
    return deleted


def _check(found: dict, reader: _Reader) -> Checked:
    checked = Checked(found)
    try:
        research.check_finding(found)
    except Rejected as rejected:
        checked.problem = rejected.reason
        return checked
    for source in found["sources"]:
        link = source["link"].strip()
        read = reader.read(link)
        if read["error"] is not None:
            continue
        if not verify(source["passage"], read["text"]):
            checked.problem = (
                f"the passage relied on from {link} is not in the fetched text"
            )
            return checked
        checked.sources.append(
            {
                **source,
                "link": link,
                "retrieved_on": read["retrieved_on"],
                "domain": registrable_domain(link) or FOLDERS,
            }
        )
    if not checked.sources:
        checked.problem = "none of its sources could be read"
    return checked


class _Finding:
    """Keeps one checked finding: as a candidate, a note, or not at all."""

    def __init__(self, conn: sqlite3.Connection, step: Step, r: Reviewed, c: Checked):
        self.conn = conn
        self.step = step
        self.r = r
        self.c = c
        self.claim = c.found["claim"].strip()

    def reject(self, reason: str) -> None:
        steps.record_rejection(
            self.conn,
            self.step.run,
            self.step.key,
            "currency_finding",
            self.r.target,
            reason,
            {"claim": self.claim[:200]},
        )

    def identity(self) -> str:
        """The equivalent finding the researcher named, if it is one for this
        target; otherwise the same claim for the same target has the same id."""
        named = self.c.found["equivalent_finding"]
        if (
            named
            and self.conn.execute(
                "SELECT 1 FROM currency_findings WHERE id = ? AND target = ?",
                (named, self.r.target),
            ).fetchone()
        ):
            return named
        return knowledge.new_id("cf", self.r.target, knowledge.same_text(self.claim))

    def keep(self) -> str | None:
        """The candidate made, if any."""
        if self.c.problem is not None:
            self.reject(self.c.problem)
            return None
        conn, run_id = self.conn, self.step.run
        finding_id = self.identity()
        earlier = conn.execute(
            "SELECT * FROM currency_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        assessment = self.c.found["assessment"]
        count = independent(self.c.sources)
        needed = self.r.cfg["thresholds"]["currency_min_sources"]
        change = assessment in CHANGE_ASSESSMENTS and count >= needed
        if earlier is not None:
            refused = self._refused(earlier, change)
            if refused:
                self.reject(f"{finding_id} {refused}")
                return None
        conn.execute(
            "INSERT INTO currency_findings (id, run, target, claim, assessment,"
            " sources, status, reason, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET claim = excluded.claim,"
            " assessment = excluded.assessment, sources = excluded.sources,"
            " status = excluded.status, reason = excluded.reason,"
            " updated_at = excluded.updated_at",
            (
                finding_id,
                run_id,
                self.r.target,
                self.claim,
                assessment,
                json.dumps(self.c.sources),
                "candidate" if change else "note",
                None if change else _note_reason(assessment, count, needed),
                self.r.now,
                self.r.now,
            ),
        )
        if not change:
            return None
        candidate_id = knowledge.new_id("c", run_id, finding_id)
        conn.execute(
            "INSERT OR IGNORE INTO candidates (id, run, proposal, repo, summary,"
            " evidence, signals, reason, created_at, currency_finding_id)"
            " VALUES (?, ?, 'change', ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                run_id,
                config.split_target(self.r.target)[0],
                self.claim,
                json.dumps([finding_id]),
                json.dumps({"sources": count}),
                assessment,
                self.r.now,
                finding_id,
            ),
        )
        return candidate_id

    def _refused(self, earlier: Mapping, change: bool) -> str | None:
        """Why an earlier finding with this identity is not kept again."""
        status = earlier["status"]
        if status == "proposed":
            return "is already proposed as a change"
        if status == "candidate" and earlier["run"] == self.step.run:
            return "repeats a finding of this dream"
        if status == "dismissed":
            cited = {s["domain"] for s in json.loads(earlier["sources"])}
            new = {s["domain"] for s in self.c.sources} - cited
            if not (change and new):
                return "was dismissed; it returns only with a new independent source"
        return None


def _note_reason(assessment: str, count: int, needed: int) -> str:
    if assessment == "current":
        return "the sources find the target current"
    if assessment not in CHANGE_ASSESSMENTS:
        return "the finding does not call for a change to the target"
    sources = "source" if count == 1 else "sources"
    return f"{count} independent {sources} of the {needed} needed for a change (16.3)"

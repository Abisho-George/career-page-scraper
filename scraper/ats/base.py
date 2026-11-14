"""Common Job dataclass and helpers shared by every extractor."""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import asdict, dataclass, field

from selectolax.lexbor import LexborHTMLParser

import config


class AtsError(Exception):
    """An ATS API call failed (server error, bad JSON, ...). Usually transient."""


class AtsBoardGone(AtsError):
    """The board itself is gone or invalid: HTTP 404/410 or another permanent 4xx (400, 401,
    422, ...), or a 2xx response that is not a job board (not JSON, or e.g. {"success": false}).
    Means the company moved ATS or removed the board."""


class BlockedError(Exception):
    """Raised when a fetch hits bot protection. Never retried. `status` is the company status
    it maps to, so callers can write `outcome.status = e.status` for this and its subclasses."""

    status = "BLOCKED"

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RobotsDisallowedError(BlockedError):
    """robots.txt disallows the URL. A subclass of BlockedError so code that only needs to
    'stop fetching' can catch both, but it maps to its own status."""

    status = "ROBOTS_DISALLOWED"


@dataclass
class Job:
    title: str
    apply_url: str
    location: str = ""
    department: str = ""
    employment_type: str = ""
    description: str = ""
    posted_at: str = ""
    external_id: str = ""
    source_method: str = ""
    remote: str = ""  # remote | hybrid | onsite | "" (filled by ATS or normalize.py)
    extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section|article)\b[^>]*>", re.I)


def html_to_text(raw: str | None, max_chars: int = config.DESCRIPTION_MAX_CHARS) -> str:
    """Strip HTML to readable plain text. Handles entity-escaped HTML (Greenhouse sends that)."""
    if not raw:
        return ""
    s = raw
    if "&lt;" in s and "<" not in s:
        s = htmllib.unescape(s)
    if "<" in s:
        s = _BLOCK_TAGS.sub("\n", s)
        tree = LexborHTMLParser(f"<div>{s}</div>")
        tree.strip_tags(["script", "style", "noscript"])
        node = tree.body or tree.root
        s = node.text(separator="", strip=False) if node else ""
    s = htmllib.unescape(s).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()[:max_chars]


async def get_json(fetcher, url: str, *, method: str = "GET", payload: dict | None = None, what: str = "ats api"):
    """Fetch JSON from an ATS API. 403/429/503 -> BlockedError; other 4xx (except 408) or a 2xx
    that isn't JSON -> AtsBoardGone; 5xx / 408 -> AtsError (transient).
    robots.txt disallow -> RobotsDisallowedError."""
    if method == "POST":
        resp = await fetcher.post_json(url, payload or {})
    else:
        resp = await fetcher.get(url, headers={"Accept": "application/json"})
    if resp.status in (403, 429, 503):
        raise BlockedError(f"http {resp.status} on {what}")
    if 400 <= resp.status < 500 and resp.status != 408:
        raise AtsBoardGone(f"{what} http {resp.status}")
    if not resp.ok:
        raise AtsError(f"{what} http {resp.status}")
    try:
        return resp.json()
    except ValueError as e:
        raise AtsBoardGone(f"{what} returned a non-JSON response") from e


def require_board(data, key: str, what: str) -> dict:
    """A real board response is a JSON object with `key`; anything else means the board is gone."""
    if not isinstance(data, dict) or key not in data:
        raise AtsBoardGone(f"{what}: invalid board response (no '{key}')")
    return data


class DetailPlan:
    """Which jobs get a (slow) per-job detail call this run, within the budget
    (config.MAX_DETAIL_FETCHES per company per run, Workday / SmartRecruiters / Workable):

      1. jobs we have never stored (new), in listing order;
      2. stored jobs whose description is still empty, oldest first_seen first.

    Jobs that already have a description are skipped. A detail call that fails leaves the
    description empty, so that job is retried on a later run. Built by diff.detail_plan()."""

    def __init__(self, job_key=None, stored_empty: dict[str, str] | None = None,
                 stored_full: set[str] | None = None, limit: int | None = None):
        self.job_key = job_key or (lambda job: job.external_id or job.apply_url)
        self.stored_empty = stored_empty or {}    # job key -> first_seen
        self.stored_full = stored_full or set()    # job keys that already have a description
        self.limit = config.MAX_DETAIL_FETCHES if limit is None else limit

    def pick(self, jobs: list[Job]) -> list[Job]:
        new: list[Job] = []
        backlog: list[tuple[str, int, Job]] = []
        seen: set[str] = set()
        for i, job in enumerate(jobs):
            key = self.job_key(job)
            if key in seen or key in self.stored_full:
                continue
            seen.add(key)
            if key in self.stored_empty:
                backlog.append((self.stored_empty[key] or "", i, job))
            else:
                new.append(job)
        backlog.sort(key=lambda t: (t[0], t[1]))
        return (new + [job for _, _, job in backlog])[: max(self.limit, 0)]


def join_nonempty(*parts: str | None, sep: str = ", ") -> str:
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return sep.join(out)

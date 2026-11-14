"""Common Job dataclass and helpers shared by every extractor."""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import asdict, dataclass, field

from selectolax.lexbor import LexborHTMLParser

import config


class AtsError(Exception):
    """An ATS API call failed in a way that means 'this token/endpoint does not work'."""


class BlockedError(Exception):
    """Raised when a fetch hits bot protection or a robots.txt disallow. Never retried."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    """Fetch JSON from an ATS API. 403/429/503 -> BlockedError; other non-2xx or bad JSON -> AtsError."""
    if method == "POST":
        resp = await fetcher.post_json(url, payload or {})
    else:
        resp = await fetcher.get(url, headers={"Accept": "application/json"})
    if resp.status in (403, 429, 503):
        raise BlockedError(f"http {resp.status} on {what}")
    if not resp.ok:
        raise AtsError(f"{what} http {resp.status}")
    try:
        return resp.json()
    except ValueError as e:
        raise AtsError(f"{what} returned invalid json") from e


def never_known(_external_id: str) -> bool:
    return False


def join_nonempty(*parts: str | None, sep: str = ", ") -> str:
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return sep.join(out)

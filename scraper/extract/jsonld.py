"""schema.org JobPosting from <script type="application/ld+json">."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from scraper.ats.base import Job, html_to_text, join_nonempty

log = logging.getLogger(__name__)


def _load(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    raw = re.sub(r"^\s*<!--|-->\s*$", "", raw)
    try:
        return json.loads(raw)
    except ValueError:
        # common breakage: raw newlines/tabs inside strings, trailing commas
        fixed = re.sub(r"[\r\n\t]+", " ", raw)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            return json.loads(fixed)
        except ValueError:
            return None


def _walk(node):
    """Yield every dict in a JSON-LD structure (handles @graph, lists, nesting)."""
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity", "hasPart"):
            if key in node:
                yield from _walk(node[key])


def _is_job(d: dict) -> bool:
    t = d.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower().endswith("jobposting") for x in types if x)


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("name") or v.get("@value") or v.get("value") or "")
    if isinstance(v, list):
        return ", ".join(_text(x) for x in v if _text(x))
    return str(v)


def _location(d: dict) -> tuple[str, str]:
    remote = ""
    if str(d.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        remote = "remote"
    locs = d.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    out = []
    for loc in locs if isinstance(locs, list) else []:
        if not isinstance(loc, dict):
            out.append(_text(loc))
            continue
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            out.append(addr)
            continue
        out.append(join_nonempty(_text(addr.get("addressLocality")), _text(addr.get("addressRegion")),
                                 _text(addr.get("addressCountry"))) or _text(loc.get("name")))
    if not out and d.get("applicantLocationRequirements"):
        out.append(_text(d["applicantLocationRequirements"]))
    return join_nonempty(*out, sep=" / "), remote


def parse_jobposting(d: dict, page_url: str, method: str = "jsonld") -> Job | None:
    title = html_to_text(_text(d.get("title") or d.get("name")))
    if not title:
        return None
    location, remote = _location(d)
    url = d.get("url") or d.get("sameAs") or page_url
    if isinstance(url, list):
        url = url[0] if url else page_url
    ident = d.get("identifier")
    ext = ""
    if isinstance(ident, dict):
        ext = str(ident.get("value") or "")
    elif isinstance(ident, (str, int)):
        ext = str(ident)
    emp = d.get("employmentType")
    if isinstance(emp, list):
        emp = ", ".join(str(e) for e in emp)
    return Job(
        title=title,
        apply_url=urljoin(page_url, str(url)),
        location=location,
        department=_text(d.get("occupationalCategory") or d.get("industry"))[:200],
        employment_type=str(emp or "").replace("_", " ").title(),
        description=html_to_text(_text(d.get("description"))),
        posted_at=str(d.get("datePosted") or ""),
        external_id=ext,
        source_method=method,
        remote=remote,
    )


def extract_jobs(html: str, page_url: str, method: str = "jsonld") -> list[Job]:
    if not html or "ld+json" not in html:
        return []
    tree = LexborHTMLParser(html)
    jobs: list[Job] = []
    seen: set[tuple[str, str]] = set()
    for script in tree.css('script[type="application/ld+json"]'):
        data = _load(script.text())
        if data is None:
            continue
        for d in _walk(data):
            if not _is_job(d):
                continue
            job = parse_jobposting(d, page_url, method)
            if job and (job.title, job.apply_url) not in seen:
                seen.add((job.title, job.apply_url))
                jobs.append(job)
    return jobs

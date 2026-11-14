"""Heuristic extraction for custom careers pages: find job-detail links, fetch each politely,
take JSON-LD if the detail page has it, else <h1> + trafilatura text + a location guess."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser

import config
from scraper.ats.base import BlockedError, Job, html_to_text
from scraper.blockcheck import is_blocked
from scraper.extract import jsonld

log = logging.getLogger(__name__)

JOB_PATH = re.compile(
    r"/(?:job|jobs|careers?|positions?|openings?|vacanc(?:y|ies)|roles?|opportunit(?:y|ies)|"
    r"requisitions?|job-?details?|posting|postings)/([^?#]+)", re.I)
JOB_QUERY_KEYS = re.compile(r"^(id|jobid|job_id|job-id|gh_jid|jid|req|reqid|requisitionid|posting_?id|pid)$", re.I)
EXCLUDE_PATH = re.compile(
    r"/(blog|news|press|media|events?|stories|story|insights|resources|webinars?|podcasts?|case-studies|"
    r"tag|tags|category|categories|author|investors?|legal|privacy|cookie|terms|login|signin|sign-in|"
    r"signup|register|alerts?|job-alerts?|talent-community|saved-jobs)(/|$)", re.I)
CATEGORY_SLUGS = {
    "search", "all", "teams", "team", "departments", "department", "locations", "location", "benefits",
    "culture", "students", "student", "interns", "internships", "internship", "university", "graduates",
    "faq", "faqs", "life", "perks", "values", "diversity", "inclusion", "apply", "engineering", "sales",
    "marketing", "design", "product", "operations", "finance", "people", "hr", "support", "data", "it",
    "early-careers", "experienced-professionals", "open-positions", "current-openings", "openings", "jobs",
    "careers", "job", "positions", "vacancies", "roles", "en", "us", "uk", "in", "index", "home",
    "how-we-hire", "hiring-process", "our-process", "why-join-us", "working-here", "remote",
}
WEAK_SLUG_PREFIX = ("life-", "why-", "our-", "how-", "meet-", "working-at", "work-at", "join-", "about-")
NEXT_TEXT = re.compile(r"^\s*(next|next page|more jobs|›|»|>|→)\s*$", re.I)
LOCATION_LABEL = re.compile(r"\b(?:job\s+)?locations?\s*[:\-–|]\s*([^\n|•]{2,80})", re.I)
APPLY_SIGNAL = re.compile(r"\bapply\b|\bsubmit (your )?application\b|\bjob description\b|"
                          r"\bresponsibilities\b|\brequirements\b|\bqualifications\b", re.I)


def _same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _clean_url(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "gclid", "fbclid", "source", "ref"))]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(q), ""))


def is_job_link(url: str, careers_url: str, domain: str) -> bool:
    if not _same_site(url, domain):
        return False
    p = urlsplit(url)
    path = p.path.rstrip("/")
    if path == urlsplit(careers_url).path.rstrip("/") and not p.query:
        return False
    if EXCLUDE_PATH.search(path) or re.search(r"\.(pdf|jpg|png|zip|docx?)$", path, re.I):
        return False
    if any(JOB_QUERY_KEYS.match(k) for k, _ in parse_qsl(p.query)) and re.search(
            r"job|career|position|opening|vacanc|role|requisition", path, re.I):
        return True
    m = JOB_PATH.search(path + "/")
    if not m:
        return False
    rest = m.group(1).strip("/")
    if not rest:
        return False
    slug = rest.split("/")[-1].lower()
    if slug in CATEGORY_SLUGS or slug.startswith(WEAK_SLUG_PREFIX):
        return False
    if "/job/" in path.lower() + "/":
        return True
    # a real job slug usually has an id or several words; category pages are a single word
    return bool(re.search(r"\d", slug)) or slug.count("-") + slug.count("_") >= 1 or len(slug) >= 20


def collect_job_links(html: str, page_url: str, careers_url: str, domain: str) -> dict[str, str]:
    """{url: anchor text} for every job-looking link on the page."""
    tree = LexborHTMLParser(html)
    links: dict[str, str] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = _clean_url(urljoin(page_url, href))
        if url in links or not is_job_link(url, careers_url, domain):
            continue
        links[url] = re.sub(r"\s+", " ", a.text(separator=" ", strip=True))[:200]
    return links


def next_page_url(html: str, page_url: str, domain: str, seen: set[str]) -> str | None:
    tree = LexborHTMLParser(html)
    cands = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        rel = (a.attributes.get("rel") or "").lower()
        label = a.text(strip=True) or a.attributes.get("aria-label") or ""
        if "next" in rel or NEXT_TEXT.match(label) or (a.attributes.get("aria-label") or "").lower().startswith("next"):
            cands.append(urljoin(page_url, href))
    # fall back to ?page=N+1 links
    cur = dict(parse_qsl(urlsplit(page_url).query)).get("page", "1")
    if cur.isdigit():
        want = str(int(cur) + 1)
        for a in tree.css("a[href]"):
            u = urljoin(page_url, a.attributes.get("href") or "")
            if dict(parse_qsl(urlsplit(u).query)).get("page") == want:
                cands.append(u)
    for c in cands:
        c = _clean_url(c)
        if _same_site(c, domain) and c not in seen:
            return c
    return None


def guess_location(text: str) -> str:
    m = LOCATION_LABEL.search(text or "")
    if m:
        return m.group(1).strip(" .,;")
    head = (text or "")[:3000]
    for city in config.KNOWN_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", head):
            return city
    return ""


def _first_h1(tree: LexborHTMLParser) -> str:
    for sel in ("h1", "[class*=job-title]", "[class*=jobTitle]", "h2"):
        n = tree.css_first(sel)
        if n:
            t = re.sub(r"\s+", " ", n.text(separator=" ", strip=True))
            if 2 <= len(t) <= 200:
                return t
    return ""


async def parse_detail(html: str, url: str, anchor_text: str, method: str) -> Job | None:
    found = jsonld.extract_jobs(html, url, method=method)
    if found:
        job = found[0]
        if urlsplit(job.apply_url).netloc != urlsplit(url).netloc:
            job.extra["listing_url"] = url
        return job
    tree = LexborHTMLParser(html)
    title = _first_h1(tree) or anchor_text
    if not title:
        t = tree.css_first("title")
        title = t.text(strip=True).split("|")[0].strip() if t else ""
    text = await asyncio.to_thread(trafilatura.extract, html, url=url, include_comments=False,
                                   include_tables=True, favor_recall=True) or ""
    tree.strip_tags(["script", "style", "noscript", "template"])
    body = tree.body.text(separator="\n", strip=True) if tree.body else ""
    if not title or not APPLY_SIGNAL.search(text + "\n" + body):
        return None
    return Job(
        title=title,
        apply_url=url,
        location=guess_location(body) or guess_location(text),
        description=html_to_text(text),
        source_method=method,
    )


async def extract_jobs(html: str, page_url: str, domain: str, fetcher, method: str = "heuristic",
                       careers_url: str | None = None) -> list[Job]:
    careers_url = careers_url or page_url
    links: dict[str, str] = {}
    seen_pages = {_clean_url(page_url)}
    cur_html, cur_url = html, page_url
    for _ in range(config.HEURISTIC_MAX_PAGES):
        for u, t in collect_job_links(cur_html, cur_url, careers_url, domain).items():
            links.setdefault(u, t)
        if len(links) >= config.HEURISTIC_MAX_LINKS:
            break
        nxt = next_page_url(cur_html, cur_url, domain, seen_pages)
        if not nxt:
            break
        seen_pages.add(nxt)
        try:
            resp = await fetcher.get(nxt)
        except (BlockedError, httpx.HTTPError):
            break
        if not resp.ok or is_blocked(resp)[0]:
            break
        cur_html, cur_url = resp.text, resp.url

    jobs: list[Job] = []
    for url, anchor in list(links.items())[: config.HEURISTIC_MAX_LINKS]:
        try:
            resp = await fetcher.get(url)
        except BlockedError:
            break  # robots.txt says no: stop fetching detail pages, keep what we have
        except httpx.HTTPError as e:
            log.debug("detail %s failed: %s", url, type(e).__name__)
            continue
        if is_blocked(resp)[0]:
            log.debug("detail pages blocked at %s; stopping", url)
            break
        if not resp.ok:
            continue
        job = await parse_detail(resp.text, resp.url, anchor, method)
        if job:
            jobs.append(job)
    return jobs

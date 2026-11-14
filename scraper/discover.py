"""Find a company's careers page."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

import config
from scraper.ats.base import BlockedError
from scraper.blockcheck import is_blocked
from scraper.detect import collect_urls, is_ats_url
from scraper.fetch import Fetcher, Response

log = logging.getLogger(__name__)

CAREERS_WORDS = re.compile(
    r"\b(careers?|jobs?|openings?|open (?:positions|roles)|positions?|vacanc(?:y|ies)|join (?:us|our team)|"
    r"we'?re hiring|work with us|hiring|opportunit(?:y|ies))\b", re.I)
LINK_TEXT = re.compile(
    r"\b(careers?|jobs|join us|join our team|we'?re hiring|work with us|openings|open positions|"
    r"open roles|vacancies|work at)\b", re.I)
LINK_PATH = re.compile(r"(careers?|jobs|join-?us|work-?with-?us|openings|vacancies|hiring)", re.I)
NOT_FOUND = re.compile(r"\b(404|not found|page not found|doesn'?t exist|no longer available)\b", re.I)
EXTERNAL_BOARDS = ("linkedin.com", "naukri.com", "indeed.", "glassdoor.", "internshala.com",
                   "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "angel.co",
                   "wellfound.com", "monster.", "foundit.in", "shine.com", "apps.apple.com", "play.google.com")


@dataclass
class Discovery:
    url: str | None
    reason: str
    page: Response | None = None   # the careers page response when we already fetched it
    blocked: str = ""              # first bot-protection reason seen during discovery
    homepage: Response | None = None


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _title_and_headings(html: str) -> str:
    tree = LexborHTMLParser(html)
    parts = []
    for sel in ("title", "h1", "h2", "meta[property='og:title']"):
        for n in tree.css(sel)[:5]:
            parts.append(n.attributes.get("content") or n.text(separator=" ", strip=True))
    return " ".join(p for p in parts if p)


def looks_like_careers(resp: Response, domain: str) -> bool:
    if not resp.ok or not resp.text:
        return False
    final = resp.url
    if is_ats_url(final):
        return True
    parts = urlsplit(final)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    on_jobs_subdomain = host.split(".")[0] in config.CAREERS_SUBDOMAINS
    if not path and not on_jobs_subdomain:
        return False  # redirected back to the homepage
    heads = _title_and_headings(resp.text)
    if NOT_FOUND.search(heads):
        return False
    if CAREERS_WORDS.search(heads) or LINK_PATH.search(path) or on_jobs_subdomain:
        return True
    return any(is_ats_url(u) for u in collect_urls(final, resp.text))


def best_homepage_link(home: Response, domain: str) -> str | None:
    """ATS link anywhere on the homepage wins; otherwise the best careers-ish <a>, same-site first."""
    tree = LexborHTMLParser(home.text)
    best, best_score = None, 0
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(home.url, href)
        if is_ats_url(url):
            return url
        if any(b in url.lower() for b in EXTERNAL_BOARDS):
            continue
        text = a.text(separator=" ", strip=True)[:80]
        label = f"{text} {a.attributes.get('title') or ''} {a.attributes.get('aria-label') or ''}"
        path = urlsplit(url).path
        score = 0
        if LINK_TEXT.search(label):
            score += 3
            if text.strip().lower() in ("careers", "jobs", "careers at", "join us", "we're hiring"):
                score += 2
        if LINK_PATH.search(path):
            score += 2
        if score == 0:
            continue
        if same_site(url, domain):
            score += 1
        if path.rstrip("/") == "":
            score -= 2
        if score > best_score or (score == best_score and best and len(url) < len(best)):
            best, best_score = url, score
    return best


async def _try(fetcher: Fetcher, url: str) -> tuple[Response | None, str]:
    """Fetch, returning (response, blocked_reason). Network errors -> (None, '')."""
    try:
        resp = await fetcher.get(url)
    except BlockedError as e:
        return None, e.reason
    except httpx.HTTPError as e:
        log.debug("discover %s failed: %s", url, type(e).__name__)
        return None, ""
    blocked, reason = is_blocked(resp)
    if blocked and resp.status != 404:
        return resp, reason
    return resp, ""


async def discover(domain: str, fetcher: Fetcher, sheet_url: str | None = None) -> Discovery:
    if sheet_url:
        return Discovery(sheet_url, "from sheet")

    first_blocked = ""
    hosts = [domain]
    tried: set[str] = set()

    # 1) common paths and subdomains
    candidates = [f"https://{{host}}{p}" for p in config.CAREERS_PATHS]
    candidates += [f"https://{sub}.{domain}/" for sub in config.CAREERS_SUBDOMAINS]
    for tmpl in candidates:
        url = tmpl.format(host=hosts[0])
        if url in tried:
            continue
        tried.add(url)
        resp, blocked = await _try(fetcher, url)
        if resp is None and not blocked and "{host}" in tmpl and hosts[0] == domain:
            # apex domain may not resolve; switch to www. for the remaining probes
            hosts[0] = f"www.{domain}"
            url = tmpl.format(host=hosts[0])
            tried.add(url)
            resp, blocked = await _try(fetcher, url)
        if blocked:
            first_blocked = first_blocked or blocked
            continue
        if resp is not None and resp.url in tried and resp.url != url:
            continue  # redirected to a page we already rejected
        if resp is not None:
            tried.add(resp.url)
            if looks_like_careers(resp, domain):
                return Discovery(resp.url, f"probe {urlsplit(url).netloc}{urlsplit(url).path}", page=resp)

    # 2) homepage links
    home, blocked = await _try(fetcher, f"https://{hosts[0]}/")
    if home is None and not blocked and hosts[0] == domain:
        home, blocked = await _try(fetcher, f"https://www.{domain}/")
    if blocked:
        first_blocked = first_blocked or blocked
        return Discovery(None, f"homepage blocked: {blocked}", blocked=first_blocked)
    if home is None:
        return Discovery(None, "homepage unreachable", blocked=first_blocked)
    if not home.ok:
        return Discovery(None, f"homepage http {home.status}", blocked=first_blocked, homepage=home)
    link = best_homepage_link(home, domain)
    if link:
        return Discovery(link, "homepage link", blocked=first_blocked, homepage=home)
    reason = "no careers link on homepage"
    return Discovery(None, reason, blocked=first_blocked, homepage=home)


async def find_careers_url(domain: str, fetcher: Fetcher, sheet_url: str | None = None) -> tuple[str | None, str]:
    d = await discover(domain, fetcher, sheet_url)
    return d.url, d.reason

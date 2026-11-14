"""Identify which ATS (job platform) a careers page uses, and the company token on that ATS."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

SUPPORTED = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "workday")
FLAG_ONLY = ("darwinbox", "keka", "zohorecruit", "freshteam", "icims", "taleo", "successfactors", "bamboohr")

_TOKEN = r"([A-Za-z0-9][A-Za-z0-9_.\-%]*)"
_RESERVED = {
    "embed", "v1", "v0", "api", "j", "jobs", "job", "careers", "search", "static", "assets", "js",
    "css", "img", "images", "www", "app", "apply", "oneclick-ui", "sr-jobs", "posting-api",
    "favicon.ico", "robots.txt", "en", "en-us", "login", "signup", "privacy", "terms", "cdn",
}

# (ats, compiled pattern). Order matters: most specific first.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?[^\"'\s<>]*\bfor=" + _TOKEN, re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?[^\"'\s<>]*\bfor=" + _TOKEN, re.I)),
    ("greenhouse", re.compile(r"(?:job-boards(?:\.eu)?|boards(?:\.eu)?)\.greenhouse\.io/" + _TOKEN, re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _TOKEN, re.I)),
    ("lever", re.compile(r"jobs\.(eu\.)?lever\.co/" + _TOKEN, re.I)),
    ("lever", re.compile(r"api\.(eu\.)?lever\.co/v0/postings/" + _TOKEN, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _TOKEN, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _TOKEN, re.I)),
    ("workday", re.compile(
        r"([a-z0-9][a-z0-9\-]*)\.(wd\d+)\.myworkdayjobs\.com/wday/cxs/[a-z0-9\-]+/([A-Za-z0-9_\-]+)")),
    ("workday", re.compile(
        r"([a-z0-9][a-z0-9\-]*)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")),
    ("workday", re.compile(
        r"(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([a-z0-9\-]+)/([A-Za-z0-9_\-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/" + _TOKEN, re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _TOKEN, re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?" + _TOKEN, re.I)),
    ("workable", re.compile(r"//([a-z0-9][a-z0-9\-]*)\.workable\.com", re.I)),
    ("recruitee", re.compile(r"//([a-z0-9][a-z0-9\-]*)\.recruitee\.com", re.I)),
]

_FLAG_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("darwinbox", re.compile(r"//([a-z0-9\-]+)\.darwinbox\.(?:in|com)", re.I)),
    ("keka", re.compile(r"//([a-z0-9\-]+)\.keka\.com", re.I)),
    ("zohorecruit", re.compile(r"//([a-z0-9\-]+)\.zohorecruit\.(?:com|in|eu)", re.I)),
    ("freshteam", re.compile(r"//([a-z0-9\-]+)\.freshteam\.com", re.I)),
    ("icims", re.compile(r"//([a-z0-9\-]+)\.icims\.com", re.I)),
    ("taleo", re.compile(r"//([a-z0-9\-]+)\.taleo\.net", re.I)),
    ("successfactors", re.compile(r"//([a-z0-9\-.]+)\.successfactors\.(?:com|eu)", re.I)),
    ("successfactors", re.compile(r"//(jobs\.sap\.com)", re.I)),
    ("bamboohr", re.compile(r"//([a-z0-9\-]+)\.bamboohr\.com", re.I)),
]


def _clean_token(ats: str, token: str) -> str | None:
    token = unquote(token).strip().strip("/.")
    if not token or token.lower() in _RESERVED:
        return None
    if ats in ("workable", "recruitee") and token.lower() in ("www", "apply", "api", "app", "resources", "help"):
        return None
    return token


def _match_url(url: str) -> tuple[str, str] | None:
    for ats, pat in _PATTERNS:
        m = pat.search(url)
        if not m:
            continue
        if ats == "workday":
            g = m.groups()
            if pat.pattern.startswith("(wd"):  # myworkdaysite.com/recruiting/{tenant}/{site}
                wd, tenant, site = g
                return ats, f"{tenant.lower()}|{wd.lower()}|{site}|site"
            tenant, wd, site = g
            if site.lower() in ("wday", "job"):
                continue
            return ats, f"{tenant.lower()}|{wd.lower()}|{site}"
        if ats == "lever":
            eu, tok = m.groups()
            tok = _clean_token(ats, tok)
            if tok:
                return ats, f"eu/{tok}" if eu else tok
            continue
        tok = _clean_token(ats, m.groups()[-1])
        if tok:
            return ats, tok
    return None


def _match_flag(url: str) -> tuple[str, str] | None:
    for ats, pat in _FLAG_PATTERNS:
        m = pat.search(url)
        if m:
            return ats, m.group(1).lower()
    return None


def collect_urls(base_url: str, html: str) -> list[str]:
    """All hrefs, iframe srcs and script srcs (absolute) plus raw URL-looking strings in inline scripts."""
    urls: list[str] = []
    if not html:
        return urls
    tree = LexborHTMLParser(html)
    for sel, attr in (("a[href]", "href"), ("iframe[src]", "src"), ("script[src]", "src"),
                      ("link[href]", "href"), ("form[action]", "action"), ("[data-src]", "data-src")):
        for node in tree.css(sel):
            v = (node.attributes.get(attr) or "").strip()
            if v and not v.startswith(("javascript:", "mailto:", "tel:", "#")):
                urls.append(urljoin(base_url, v))
    # Inline scripts often build the embed URL in JS: grab ATS-looking strings.
    for m in re.finditer(r"(?:https?:)?//[A-Za-z0-9.\-]+\.(?:greenhouse\.io|lever\.co|ashbyhq\.com|"
                         r"myworkdayjobs\.com|myworkdaysite\.com|smartrecruiters\.com|workable\.com|"
                         r"recruitee\.com)[^\"'\s<>\\)]*", html):
        u = m.group(0)
        urls.append("https:" + u if u.startswith("//") else u)
    return urls


def detect_ats(careers_url: str, html: str = "") -> tuple[str, str]:
    """Returns (ats, token). ats is one of SUPPORTED, FLAG_ONLY, 'custom' (page found, no ATS)
    or 'unknown' (nothing to look at). token is '' when there is none."""
    candidates = [careers_url] + collect_urls(careers_url, html)
    for u in candidates:
        hit = _match_url(u)
        if hit:
            return hit
    for u in candidates:
        hit = _match_flag(u)
        if hit:
            return hit
    # Greenhouse embed: <div id="grnhse_app"> + Grnhse.Settings / ?gh_jid with a board token in JS
    if html and "grnhse" in html:
        m = re.search(r"[?&]for=([A-Za-z0-9_\-]+)", html)
        if m:
            return "greenhouse", m.group(1)
    if not html:
        return "unknown", ""
    return "custom", ""


def ats_job_board_url(ats: str, token: str) -> str:
    """Public board URL for a detected ATS (used as the careers_url when we only have the token)."""
    if ats == "greenhouse":
        return f"https://job-boards.greenhouse.io/{token}"
    if ats == "lever":
        return f"https://jobs.eu.lever.co/{token[3:]}" if token.startswith("eu/") else f"https://jobs.lever.co/{token}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{token}"
    if ats == "smartrecruiters":
        return f"https://jobs.smartrecruiters.com/{token}"
    if ats == "workable":
        return f"https://apply.workable.com/{token}/"
    if ats == "recruitee":
        return f"https://{token}.recruitee.com/"
    if ats == "workday":
        parts = token.split("|")
        if len(parts) == 4:
            tenant, wd, site, _ = parts
            return f"https://{wd}.myworkdaysite.com/recruiting/{tenant}/{site}"
        tenant, wd, site = parts
        return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    return ""


def is_ats_url(url: str) -> bool:
    return _match_url(url) is not None


def query_param(url: str, name: str) -> str:
    return (parse_qs(urlsplit(url).query).get(name) or [""])[0]

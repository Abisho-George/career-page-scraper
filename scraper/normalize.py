"""Clean job fields: whitespace, city aliases, remote/hybrid flag, ISO posted date."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from dateutil import parser as dateparser

import config
from scraper.ats.base import Job

_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(config.CITY_ALIASES, key=len, reverse=True)) + r")\b",
    re.I,
)
_SHORT_ALIASES = {"sf", "la"}  # only replace these when they are the whole location part

REMOTE_WORDS = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed|telecommute|home[- ]based)\b", re.I)
HYBRID_WORDS = re.compile(r"\bhybrid\b", re.I)
ONSITE_WORDS = re.compile(r"\b(on[- ]?site|in[- ]office|office[- ]based)\b", re.I)
REL_DAYS = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def clean_location(loc: str) -> str:
    loc = clean(loc).strip(" ,;/|-")
    if not loc:
        return ""

    def repl(m: re.Match) -> str:
        key = m.group(1).lower()
        if key in _SHORT_ALIASES:
            return m.group(0)
        return config.CITY_ALIASES[key]

    parts = re.split(r"(\s*[,/|;]\s*)", loc)
    out = []
    for p in parts:
        if p.strip().lower() in _SHORT_ALIASES and p.strip() == p.strip().upper():
            out.append(config.CITY_ALIASES[p.strip().lower()])
        else:
            out.append(_ALIAS_RE.sub(repl, p))
    return "".join(out)


def detect_remote(title: str, location: str, description: str, current: str = "") -> str:
    if current in ("remote", "hybrid", "onsite"):
        return current
    head = f"{title}\n{location}"
    if HYBRID_WORDS.search(head):
        return "hybrid"
    if REMOTE_WORDS.search(head):
        return "remote"
    desc = (description or "")[:4000]
    if HYBRID_WORDS.search(desc):
        return "hybrid"
    if re.search(r"\b(fully|100%|completely)\s+remote\b|\bremote[- ](first|only)\b|\bthis (is a|role is) remote\b",
                 desc, re.I):
        return "remote"
    if ONSITE_WORDS.search(head):
        return "onsite"
    return ""


def parse_date(value, today: date | None = None) -> str:
    """Return an ISO date (YYYY-MM-DD) or '' if it can't be parsed."""
    if value in (None, ""):
        return ""
    today = today or datetime.now(timezone.utc).date()
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    s = clean(str(value))
    low = s.lower()
    if "today" in low or "just posted" in low or "hours ago" in low:
        return today.isoformat()
    if "yesterday" in low:
        return (today - timedelta(days=1)).isoformat()
    m = REL_DAYS.search(low)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    if re.fullmatch(r"\d{10,13}", s):
        return parse_date(int(s), today)
    try:
        dt = dateparser.parse(s, fuzzy=False, dayfirst=False)
    except (ValueError, OverflowError, TypeError):
        return ""
    if dt is None or dt.year < 2000 or dt.date() > today + timedelta(days=31):
        return ""
    return dt.date().isoformat()


def normalize_job(job: Job, today: date | None = None) -> Job:
    job.title = clean(job.title)[:300]
    job.location = clean_location(job.location)[:500]
    job.department = clean(job.department)[:200]
    job.employment_type = clean(job.employment_type)[:100]
    job.apply_url = clean(job.apply_url)
    job.description = (job.description or "").strip()[: config.DESCRIPTION_MAX_CHARS]
    job.posted_at = parse_date(job.posted_at, today)
    job.remote = detect_remote(job.title, job.location, job.description, job.remote)
    return job

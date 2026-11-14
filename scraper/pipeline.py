"""Runs every stage for one company: discover -> blockcheck -> detect -> ATS API or extraction
ladder -> normalize -> diff. process_company() never raises."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

import config
from scraper import db, diff
from scraper.ats import MODULES
from scraper.ats.base import AtsBoardGone, AtsError, BlockedError, Job
from scraper.blockcheck import is_blocked
from scraper.browser import Browser, looks_js_rendered
from scraper.detect import FLAG_ONLY, detect_ats, is_ats_url
from scraper.discover import discover
from scraper.extract import heuristic, jsonld
from scraper.fetch import Fetcher, Response
from scraper.normalize import normalize_job

log = logging.getLogger("scraper.pipeline")
company_log = logging.getLogger("scraper.company")

LISTING_LINK = re.compile(
    r"\b(open (positions|roles|jobs)|current (openings|vacancies|opportunities)|view (all )?(jobs|openings|"
    r"positions|roles|opportunities)|see (all )?(jobs|openings|positions|roles)|all jobs|search jobs|"
    r"job openings|browse jobs|explore (jobs|roles|opportunities)|find (a )?jobs?|job search)\b", re.I)
META_REFRESH = re.compile(r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]*content=[\"']?\s*\d+\s*;\s*url=([^\"'>\s]+)",
                          re.I)


@dataclass
class Outcome:
    status: str
    reason: str = ""
    method: str = ""
    jobs: list[Job] = field(default_factory=list)
    careers_url: str | None = None
    careers_url_source: str | None = None
    ats: str | None = None
    ats_token: str | None = None
    allow_close: bool = True              # False: never close jobs this run (e.g. the ATS call failed)
    clear_ats: bool = False               # forget the stored ats/ats_token even if none was detected
    empty_ats_runs: int | None = None     # new consecutive_empty_ats_runs value (None = leave as is)


ATS_SWITCH_NOTE = "ATS changed or removed, rediscovered"


@dataclass
class Context:
    conn: sqlite3.Connection
    fetcher: Fetcher
    browser: Browser | None
    run_id: int


class _Stop(Exception):
    """Internal: carries a final Outcome out of nested steps."""

    def __init__(self, outcome: Outcome):
        self.outcome = outcome


def _host_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


async def _get_page(ctx: Context, url: str) -> Response:
    """Fetch a page and follow one <meta refresh> redirect. BlockedError / httpx errors propagate."""
    resp = await ctx.fetcher.get(url)
    m = META_REFRESH.search(resp.text[:5000]) if resp.ok and len(resp.text) < 20000 else None
    if m:
        target = urljoin(resp.url, m.group(1).strip("'\""))
        if target != resp.url:
            log.debug("following meta refresh %s -> %s", resp.url, target)
            resp = await ctx.fetcher.get(target)
    return resp


def _listing_link(page: Response, domain: str) -> str | None:
    """On a careers landing page, the 'View open positions' style link (same site)."""
    tree = LexborHTMLParser(page.text)
    for a in tree.css("a[href]"):
        text = a.text(separator=" ", strip=True)[:80]
        if not LISTING_LINK.search(text):
            continue
        url = urljoin(page.url, (a.attributes.get("href") or "").strip())
        if url.split("#")[0] == page.url.split("#")[0]:
            continue
        host = _host_domain(url)
        if host == domain or host.endswith("." + domain) or is_ats_url(url):
            return url
    return None


async def _call_ats(ctx: Context, ats: str, token: str, plan) -> tuple[list[Job] | None, AtsError | None]:
    """(jobs, None) on success (jobs may be empty: the ATS says 0), or (None, error).
    BlockedError / RobotsDisallowedError propagate."""
    try:
        jobs = await MODULES[ats].fetch_jobs(token, ctx.fetcher, plan)
        log.debug("%s api %s -> %d jobs", ats, token, len(jobs))
        return jobs, None
    except AtsError as e:
        log.debug("%s api %s failed: %s", ats, token, e)
        return None, e


async def _try_ats(ctx: Context, ats: str, token: str, plan) -> list[Job] | None:
    """Returns jobs (possibly empty: the ATS says 0) or None when the API doesn't work for this token."""
    return (await _call_ats(ctx, ats, token, plan))[0]


async def _run_ladder(ctx: Context, pages: list[Response], domain: str, careers_url: str,
                      method_suffix: str = "") -> tuple[list[Job], str]:
    for page in pages:
        jobs = jsonld.extract_jobs(page.text, page.url, method=method_suffix or "jsonld")
        if jobs:
            return jobs, method_suffix or "jsonld"
    for page in pages:
        site = domain if _host_domain(page.url).endswith(domain) else _host_domain(page.url)
        jobs = await heuristic.extract_jobs(page.text, page.url, site, ctx.fetcher,
                                            method=method_suffix or "heuristic", careers_url=careers_url)
        if jobs:
            return jobs, method_suffix or "heuristic"
    return [], ""


async def _process(ctx: Context, company: sqlite3.Row) -> Outcome:
    out = await _run_stages(ctx, company)
    if out.clear_ats and not out.reason.startswith(ATS_SWITCH_NOTE):
        # every outcome of a run that dropped the stored ATS says so, whatever happened next
        out.reason = f"{ATS_SWITCH_NOTE}; {out.reason}" if out.reason else ATS_SWITCH_NOTE
    return out


async def _run_stages(ctx: Context, company: sqlite3.Row) -> Outcome:
    domain = company["website"]
    plan = diff.detail_plan(ctx.conn, company["id"], domain)
    out = Outcome(status="ERROR", careers_url=company["careers_url"],
                  careers_url_source=company["careers_url_source"],
                  ats=company["ats"], ats_token=company["ats_token"])

    # 0) Known ATS token from a previous run: go straight to the API.
    #    switched = "gone" (404/410/invalid board) or "empty" (0 jobs two runs in a row while the
    #    company had jobs): forget the token and rediscover in this same run, closing nothing.
    switched = ""
    old_ats = (company["ats"], company["ats_token"])
    if company["ats"] in MODULES and company["ats_token"]:
        try:
            jobs, err = await _call_ats(ctx, company["ats"], company["ats_token"], plan)
        except BlockedError as e:  # includes RobotsDisallowedError
            return Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason})
        if err is None:
            had = db.active_job_count(ctx.conn, company["id"])
            if jobs or not had:
                out.status, out.method, out.jobs, out.empty_ats_runs = "OK", "ats_api", jobs, 0
                return out
            streak = (company["consecutive_empty_ats_runs"] or 0) + 1
            if streak < 2:
                out.status, out.method, out.allow_close, out.empty_ats_runs = "OK", "ats_api", False, streak
                out.reason = (f"{company['ats']} returned 0 jobs (had {had}); "
                              "waiting one run before treating the board as removed")
                return out
            switched = "empty"
        elif isinstance(err, AtsBoardGone):
            switched = "gone"
        else:
            # transient (5xx, bad JSON, ...): keep the token, close nothing, try again next run
            return Outcome(**{**out.__dict__, "status": "ERROR", "reason": str(err), "allow_close": False})
        log.info("%s: stored %s board %r %s; rediscovering", domain, company["ats"], company["ats_token"],
                 "is gone" if switched == "gone" else "was empty twice")
        out.ats = out.ats_token = None
        out.clear_ats, out.allow_close, out.empty_ats_runs = True, False, 0
        out.reason = ATS_SWITCH_NOTE

    # 1) Careers URL: sheet > stored discovery > discover now (always discover after a switch).
    page: Response | None = None
    source = company["careers_url_source"]
    url = company["careers_url"] if source in ("sheet", "discovered") else None
    if switched and source != "sheet":
        url = None
    rediscovered = False

    async def run_discovery() -> tuple[str, Response | None]:
        d = await discover(domain, ctx.fetcher)
        log.debug("discovery: %s (%s)", d.url, d.reason)
        if not d.url:
            if d.blocked:
                raise _Stop(Outcome(**{**out.__dict__, "status": "BLOCKED", "reason": d.blocked}))
            if d.robots:
                raise _Stop(Outcome(**{**out.__dict__, "status": "ROBOTS_DISALLOWED", "reason": d.robots}))
            raise _Stop(Outcome(**{**out.__dict__, "status": "NO_CAREERS_PAGE", "reason": d.reason,
                                   "careers_url": None, "careers_url_source": None}))
        if switched:
            out.careers_url = d.url
        return d.url, d.page

    try:
        if not url:
            url, page = await run_discovery()
            source, rediscovered = "discovered", True
        out.careers_url, out.careers_url_source = url, source

        # The careers URL is itself a supported ATS board: use the API without fetching the page.
        if page is None and is_ats_url(url):
            ats0, token0 = detect_ats(url, "")
            if ats0 in MODULES and token0 and (ats0, token0) != old_ats:
                try:
                    jobs = await _try_ats(ctx, ats0, token0, plan)
                except BlockedError as e:
                    raise _Stop(Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason}))
                if jobs is not None:
                    out.status, out.method, out.jobs, out.ats, out.ats_token = "OK", "ats_api", jobs, ats0, token0
                    out.empty_ats_runs = 0
                    return out

        # 2) Fetch the careers page (discovery may already have it).
        if page is None:
            try:
                page = await _get_page(ctx, url)
            except BlockedError as e:
                raise _Stop(Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason}))
            except httpx.HTTPError as e:
                if source == "discovered" and not rediscovered:
                    page = None
                else:
                    raise _Stop(Outcome(**{**out.__dict__, "status": "ERROR",
                                           "reason": f"careers page unreachable: {type(e).__name__}"}))
            if (page is None or page.status in (404, 410)) and source == "discovered" and not rediscovered:
                log.debug("stored careers url %s is gone; rediscovering", url)
                url, page = await run_discovery()
                rediscovered = True
                out.careers_url = url
                if page is None:
                    try:
                        page = await _get_page(ctx, url)
                    except BlockedError as e:
                        raise _Stop(Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason}))
    except _Stop as s:
        return s.outcome

    blocked, reason = is_blocked(page)
    if blocked:
        return Outcome(**{**out.__dict__, "status": "BLOCKED", "reason": reason})
    if page.status in (404, 410):
        return Outcome(**{**out.__dict__, "status": "NO_CAREERS_PAGE",
                          "reason": f"careers page http {page.status}" + (" (url from sheet)" if source == "sheet" else "")})
    if not page.ok:
        return Outcome(**{**out.__dict__, "status": "ERROR", "reason": f"careers page http {page.status}"})

    # 3) Detect the ATS (on the URL we asked for, the final URL and the page).
    ats, token = detect_ats(url, "") if is_ats_url(url) else detect_ats(page.url, page.text)
    pages = [page]
    if ats in ("custom", "unknown"):
        hop = _listing_link(page, domain)
        if hop:
            log.debug("following listing link %s", hop)
            try:
                hop_page = await _get_page(ctx, hop)
                if hop_page.ok and not is_blocked(hop_page)[0]:
                    pages.append(hop_page)
                    ats2, tok2 = detect_ats(hop, "") if is_ats_url(hop) else detect_ats(hop_page.url, hop_page.text)
                    if ats2 not in ("custom", "unknown"):
                        ats, token = ats2, tok2
            except (BlockedError, httpx.HTTPError) as e:
                log.debug("listing link failed: %s", e)
    out.ats, out.ats_token = ats, token or None
    log.debug("detected ats=%s token=%s", ats, token)

    # 4) ATS API. An ATS that says 0 jobs does not end the search: the careers page itself may
    #    still list jobs (e.g. a stale link to an emptied board). 0 is accepted only when the
    #    page has nothing either.
    ats_note = ""
    ats_said_empty = False
    if switched and (ats, token) == old_ats:
        # Rediscovery found the same board we just gave up on: don't call it again.
        if switched == "empty":
            ats_said_empty = True
        else:
            ats_note = f"careers page still links to the old {ats} board {token!r}"
            out.ats = out.ats_token = None  # never store a dead board again
    elif ats in MODULES and token:
        try:
            jobs = await _try_ats(ctx, ats, token, plan)
        except BlockedError as e:
            return Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason})
        if jobs:
            out.status, out.method, out.jobs, out.empty_ats_runs = "OK", "ats_api", jobs, 0
            return out
        if jobs is None:
            ats_note = f"{ats} api did not accept token {token!r}"
            # Never store a token the API just rejected: the next run would take the fast path
            # to a dead board, "switch", and flip back and forth every run.
            out.ats_token = None
        else:
            ats_said_empty = True

    # 5-6) JSON-LD, then heuristic links, on the plain HTML.
    jobs, method = await _run_ladder(ctx, pages, domain, url)

    # 7) Browser fallback for JS-rendered pages (pointless when the jobs live on a known
    #    ATS we can't extract yet, or when a supported ATS already answered "0 jobs").
    if not jobs and ctx.browser is not None and ats not in FLAG_ONLY and not ats_said_empty \
            and any(looks_js_rendered(p.text) for p in pages):
        target = pages[-1].url
        log.debug("rendering %s with browser", target)
        try:
            rendered = await ctx.browser.render(target)
        except Exception as e:  # noqa: BLE001 - browser trouble must not fail the company
            log.debug("browser failed: %s", e)
            rendered = None
        if rendered is not None:
            blocked, reason = is_blocked(rendered)
            if blocked:
                return Outcome(**{**out.__dict__, "status": "BLOCKED", "reason": f"{reason} (rendered)"})
            ats_r, tok_r = detect_ats(rendered.url, rendered.text)
            if ats_r in MODULES and tok_r and (ats_r, tok_r) not in ((ats, token), old_ats if switched else (None, None)):
                out.ats, out.ats_token = ats_r, tok_r
                try:
                    r_jobs = await _try_ats(ctx, ats_r, tok_r, plan)
                except BlockedError as e:
                    return Outcome(**{**out.__dict__, "status": e.status, "reason": e.reason})
                if r_jobs is not None:
                    out.status, out.method, out.jobs, out.empty_ats_runs = "OK", "ats_api", r_jobs, 0
                    return out
            elif ats_r in FLAG_ONLY and out.ats in ("custom", "unknown"):
                out.ats, out.ats_token = ats_r, tok_r
            jobs, method = await _run_ladder(ctx, [rendered], domain, url, method_suffix="browser")

    if jobs:
        out.status, out.method, out.jobs = "OK", method, jobs
        if ats_said_empty:
            # The linked board is empty but the page lists jobs: use the page, and don't keep the
            # empty board's token (that would send the next run straight to "0 jobs").
            out.ats_token = None
            out.empty_ats_runs = 0
            out.reason = f"{ats} board is empty; jobs taken from the careers page"
        return out

    if ats_said_empty:
        out.status, out.method, out.jobs, out.empty_ats_runs = "OK", "ats_api", [], 0
        out.ats, out.ats_token = ats, token
        if switched == "empty":
            # Still linked, empty twice, and nothing on the page: the company really has no
            # openings. Accept 0 jobs and close the old ones.
            out.allow_close = True
            out.reason = f"{ATS_SWITCH_NOTE}; same {ats} board still linked and empty, 0 jobs accepted"
        return out

    if out.ats in FLAG_ONLY:
        reason = f"{out.ats} detected; no extractor yet"
    elif ats_note:
        reason = ats_note
    else:
        reason = "no job links or JobPosting data on careers page"
    return Outcome(**{**out.__dict__, "status": "NO_JOBS_FOUND", "reason": reason})


def save_outcome(conn: sqlite3.Connection, company: sqlite3.Row, run_id: int, outcome: Outcome) -> diff.DiffResult:
    now = db.now_iso()
    domain = company["website"]
    if outcome.status == "OK":
        jobs = [normalize_job(j) for j in outcome.jobs]
        jobs = [j for j in jobs if j.title and j.apply_url]
        res = diff.apply_jobs(conn, company["id"], domain, jobs, now, company_ok=outcome.allow_close,
                              method=outcome.method)
        note = ""
        if res.drop_held:
            note = "sharp drop held, waiting one run"
        elif res.drop_accepted:
            note = f"sharp drop confirmed, {res.closed} jobs closed"
        if note:
            outcome.reason = (outcome.reason + "; " if outcome.reason else "") + note
    else:
        res = diff.DiffResult(active=db.active_job_count(conn, company["id"]))
    fields = dict(
        status=outcome.status,
        status_reason=outcome.reason[:300],
        method=outcome.method or None,
        job_count=res.active,
        last_checked=now,
        last_run_id=run_id,
    )
    if company["careers_url_source"] != "sheet":
        fields["careers_url"] = outcome.careers_url
        fields["careers_url_source"] = outcome.careers_url_source if outcome.careers_url else None
    if outcome.ats:
        fields["ats"] = outcome.ats
        fields["ats_token"] = outcome.ats_token
    elif outcome.clear_ats:
        fields["ats"] = fields["ats_token"] = None
    if outcome.empty_ats_runs is not None:
        fields["consecutive_empty_ats_runs"] = outcome.empty_ats_runs
    db.update_company(conn, company["id"], **fields)
    db.add_run_counts(conn, run_id, jobs_new=res.new, jobs_closed=res.closed)
    conn.commit()
    return res


async def process_company(ctx: Context, company: sqlite3.Row) -> Outcome:
    """Never raises: any failure becomes status=ERROR with the error type as the reason."""
    started = time.monotonic()
    domain = company["website"]
    try:
        outcome = await asyncio.wait_for(_process(ctx, company), timeout=config.COMPANY_TIMEOUT)
    except asyncio.TimeoutError:
        outcome = Outcome(status="ERROR", reason=f"timeout after {int(config.COMPANY_TIMEOUT)}s")
    except BlockedError as e:  # includes RobotsDisallowedError
        outcome = Outcome(status=e.status, reason=e.reason)
    except Exception as e:  # noqa: BLE001
        log.exception("%s crashed", domain)
        outcome = Outcome(status="ERROR", reason=f"{type(e).__name__}: {str(e)[:150]}")
    if outcome.status == "ERROR" and outcome.careers_url is None and outcome.ats is None:
        # keep what we knew about the company before this failure
        outcome.careers_url = company["careers_url"]
        outcome.careers_url_source = company["careers_url_source"]
    try:
        res = save_outcome(ctx.conn, company, ctx.run_id, outcome)
        count = res.active if outcome.status == "OK" else len(outcome.jobs)
    except Exception as e:  # noqa: BLE001
        log.exception("%s: saving failed", domain)
        ctx.conn.rollback()
        outcome = Outcome(status="ERROR", reason=f"save failed: {type(e).__name__}")
        db.update_company(ctx.conn, company["id"], status="ERROR", status_reason=outcome.reason,
                          last_checked=db.now_iso(), last_run_id=ctx.run_id)
        ctx.conn.commit()
        count = 0
    secs = time.monotonic() - started
    company_log.info("%s | %s | %s | %d | %.1fs%s", domain, outcome.method or "-", outcome.status, count, secs,
                     f" | {outcome.reason}" if outcome.reason else "")
    return outcome

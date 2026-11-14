"""Change tracking: new / still open / closed."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass

import config
from scraper import db
from scraper.ats.base import DetailPlan, Job

log = logging.getLogger(__name__)


def make_job_id(domain: str, job_key: str) -> str:
    return hashlib.sha1(f"{domain}|{job_key}".encode("utf-8")).hexdigest()


def job_id_for(domain: str, job: Job) -> str:
    return make_job_id(domain, job.external_id or job.apply_url)


def detail_plan(conn: sqlite3.Connection, company_id: int, domain: str) -> DetailPlan:
    """What the ATS modules should spend their detail-call budget on for this company:
    new jobs first, then stored jobs with an empty description, oldest first_seen first."""
    full: set[str] = set()
    empty: dict[str, str] = {}
    for job_id, desc, first_seen in conn.execute(
            "SELECT job_id, description, first_seen FROM jobs WHERE company_id = ?", (company_id,)):
        if desc:
            full.add(job_id)
        else:
            empty[job_id] = first_seen or ""
    return DetailPlan(job_key=lambda job: job_id_for(domain, job), stored_empty=empty, stored_full=full)


@dataclass
class DiffResult:
    new: int = 0
    closed: int = 0
    active: int = 0
    close_skipped: bool = False   # nothing was closed because of the sharp-drop guard
    drop_held: bool = False       # first run of a sharp drop: held, waiting one run
    drop_accepted: bool = False   # second run confirmed the drop: missing jobs were closed


def _is_sharp_drop(method: str, prev_active: int, seen: int) -> bool:
    return (method != "ats_api" and prev_active >= config.CLOSE_GUARD_MIN_PREVIOUS
            and seen < prev_active * config.CLOSE_GUARD_RATIO)


def _confirms(pending: int, seen: int) -> bool:
    return pending > 0 and abs(seen - pending) <= pending * config.DROP_CONFIRM_TOLERANCE


def apply_jobs(conn: sqlite3.Connection, company_id: int, domain: str, jobs: list[Job],
               seen_at: str, company_ok: bool, method: str) -> DiffResult:
    """Upsert this run's jobs; close jobs not seen, but only when the company finished OK.
    Never closes on BLOCKED / ERROR / NO_JOBS_FOUND.

    Sharp-drop guard (scraped pages only): if a company with >= 10 active jobs suddenly shows
    < 50% of them, nothing is closed and companies.pending_drop_count stores the new count. On
    the next OK run, a count within +/-20% of it confirms the drop (missing jobs are closed);
    a recovered count resets it to 0; a further drop is held again at the new count."""
    res = DiffResult()
    prev_active = db.active_job_count(conn, company_id)
    row = conn.execute("SELECT pending_drop_count FROM companies WHERE id = ?", (company_id,)).fetchone()
    pending = (row[0] or 0) if row else 0
    seen: set[str] = set()
    for job in jobs:
        jid = job_id_for(domain, job)
        if jid in seen:
            continue
        seen.add(jid)
        if db.upsert_job(conn, company_id, jid, job.to_dict(), seen_at) in ("new", "reopened"):
            res.new += 1
    if company_ok:
        new_pending = 0
        if _is_sharp_drop(method, prev_active, len(seen)) and not _confirms(pending, len(seen)):
            res.close_skipped = res.drop_held = True
            new_pending = len(seen)
            log.warning("%s: saw %d jobs vs %d active before via %s; holding the drop for one run",
                        domain, len(seen), prev_active, method)
        else:
            if _is_sharp_drop(method, prev_active, len(seen)):
                res.drop_accepted = True
                log.info("%s: sharp drop to %d confirmed (held at %d); closing missing jobs",
                         domain, len(seen), pending)
            res.closed = db.mark_seen(conn, company_id, seen, seen_at)
        if new_pending != pending:
            db.update_company(conn, company_id, pending_drop_count=new_pending)
    res.active = db.active_job_count(conn, company_id)
    return res

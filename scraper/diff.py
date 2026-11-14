"""Change tracking: new / still open / closed."""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass

import config
from scraper import db
from scraper.ats.base import Job

log = logging.getLogger(__name__)


def make_job_id(domain: str, job_key: str) -> str:
    return hashlib.sha1(f"{domain}|{job_key}".encode("utf-8")).hexdigest()


def job_id_for(domain: str, job: Job) -> str:
    return make_job_id(domain, job.external_id or job.apply_url)


def known_with_description(conn: sqlite3.Connection, company_id: int, domain: str):
    """Predicate for ATS modules: True if we already store this job with a description,
    so the (slow) detail call can be skipped."""
    ids = {r[0] for r in conn.execute(
        "SELECT job_id FROM jobs WHERE company_id = ? AND description != ''", (company_id,))}
    return lambda external_id: make_job_id(domain, external_id) in ids


@dataclass
class DiffResult:
    new: int = 0
    closed: int = 0
    active: int = 0
    close_skipped: bool = False


def apply_jobs(conn: sqlite3.Connection, company_id: int, domain: str, jobs: list[Job],
               seen_at: str, company_ok: bool, method: str) -> DiffResult:
    """Upsert this run's jobs; close jobs not seen, but only when the company finished OK.
    Never closes on BLOCKED / ERROR / NO_JOBS_FOUND."""
    res = DiffResult()
    prev_active = db.active_job_count(conn, company_id)
    seen: set[str] = set()
    for job in jobs:
        jid = job_id_for(domain, job)
        if jid in seen:
            continue
        seen.add(jid)
        if db.upsert_job(conn, company_id, jid, job.to_dict(), seen_at) in ("new", "reopened"):
            res.new += 1
    if company_ok:
        guard = (method != "ats_api" and prev_active >= config.CLOSE_GUARD_MIN_PREVIOUS
                 and len(seen) < prev_active * config.CLOSE_GUARD_RATIO)
        if guard:
            res.close_skipped = True
            log.warning("%s: saw %d jobs vs %d active before via %s; not closing any this run",
                        domain, len(seen), prev_active, method)
        else:
            res.closed = db.mark_seen(conn, company_id, seen, seen_at)
    res.active = db.active_job_count(conn, company_id)
    return res

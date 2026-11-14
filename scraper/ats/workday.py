"""Workday candidate-experience (CXS) JSON used by every *.myworkdayjobs.com site.

token = "{tenant}|wd{N}|{site}"            -> https://{tenant}.wd{N}.myworkdayjobs.com
token = "{tenant}|wd{N}|{site}|site"       -> https://wd{N}.myworkdaysite.com/recruiting/{tenant}/{site}

List:   POST {base}/wday/cxs/{tenant}/{site}/jobs  {"appliedFacets":{},"limit":20,"offset":N,"searchText":""}
Detail: GET  {base}/wday/cxs/{tenant}/{site}{externalPath}
Detail calls are only made for jobs we don't already have a description for (see `known`),
capped at MAX_DETAIL_FETCHES per company per run, so the first run of a big tenant fills in over a few days.
"""
from __future__ import annotations

import logging

import config
from scraper.ats.base import AtsError, Job, get_json, html_to_text, join_nonempty, never_known, require_board

log = logging.getLogger(__name__)

PAGE = 20
MAX_JOBS = 5000


def parse_token(token: str) -> tuple[str, str, str, str]:
    """Returns (api_base, public_base, tenant, site)."""
    parts = token.split("|")
    if len(parts) < 3:
        raise AtsError(f"bad workday token {token!r}")
    tenant, wd, site = parts[0], parts[1], parts[2]
    if len(parts) == 4 and parts[3] == "site":
        host = f"https://{wd}.myworkdaysite.com"
        return host, f"{host}/recruiting/{tenant}/{site}", tenant, site
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    return host, f"{host}/{site}", tenant, site


def map_posting(p: dict, public_base: str) -> Job:
    path = p.get("externalPath") or ""
    bullets = p.get("bulletFields") or []
    return Job(
        title=(p.get("title") or "").strip(),
        apply_url=f"{public_base}{path}",
        location=p.get("locationsText") or "",
        posted_at=p.get("postedOn") or "",
        external_id=str(bullets[0]) if bullets else path,
        source_method="ats_api",
        extra={"path": path},
    )


def apply_detail(job: Job, d: dict) -> None:
    info = d.get("jobPostingInfo") or {}
    job.description = html_to_text(info.get("jobDescription"))
    if info.get("location"):
        job.location = join_nonempty(info.get("location"), *(info.get("additionalLocations") or []), sep=" / ")
    job.employment_type = info.get("timeType") or job.employment_type
    if info.get("startDate"):
        job.posted_at = info["startDate"]
    if info.get("externalUrl"):
        job.apply_url = info["externalUrl"]
    remote_type = str(info.get("remoteType") or "").lower()
    if "remote" in remote_type:
        job.remote = "remote"
    elif "hybrid" in remote_type:
        job.remote = "hybrid"
    elif remote_type:
        job.remote = "onsite"


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    api_base, public_base, tenant, site = parse_token(token)
    cxs = f"{api_base}/wday/cxs/{tenant}/{site}"
    jobs: list[Job] = []
    total = None
    offset = 0
    while True:
        data = await get_json(fetcher, f"{cxs}/jobs", method="POST", what="workday api",
                              payload={"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""})
        if total is None:
            require_board(data, "jobPostings", "workday api")
            # Workday only reports the real total on the first page; later pages often say 0.
            total = int(data.get("total") or 0)
        postings = data.get("jobPostings") or []
        jobs.extend(map_posting(p, public_base) for p in postings if p.get("title"))
        offset += PAGE
        if not postings or offset >= total or offset >= MAX_JOBS:
            break
    if total and len(jobs) < total:
        log.info("workday %s: listed %d of %d postings", token, len(jobs), total)

    details = 0
    for job in jobs:
        if details >= config.MAX_DETAIL_FETCHES or known(job.external_id):
            continue
        details += 1
        try:
            apply_detail(job, await get_json(fetcher, f"{cxs}{job.extra['path']}", what="workday detail"))
        except AtsError as e:
            log.debug("workday detail %s: %s", job.external_id, e)
    return jobs

"""SmartRecruiters posting API: https://developers.smartrecruiters.com/docs/posting-api"""
from __future__ import annotations

import logging
from urllib.parse import quote

import config
from scraper.ats.base import AtsError, Job, get_json, html_to_text, join_nonempty, never_known

log = logging.getLogger(__name__)

LIST = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={limit}&offset={offset}"
DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}"
PAGE = 100


def map_summary(p: dict, token: str) -> Job:
    loc = p.get("location") or {}
    location = loc.get("fullLocation") or join_nonempty(loc.get("city"), loc.get("region"), loc.get("country"))
    remote = ""
    if loc.get("remote"):
        remote = "remote"
    elif loc.get("hybrid"):
        remote = "hybrid"
    return Job(
        title=(p.get("name") or "").strip(),
        apply_url=f"https://jobs.smartrecruiters.com/{token}/{p.get('id')}",
        location=location,
        department=((p.get("department") or {}).get("label") or "").strip(),
        employment_type=((p.get("typeOfEmployment") or {}).get("label") or "").strip(),
        posted_at=p.get("releasedDate") or "",
        external_id=str(p.get("id") or ""),
        source_method="ats_api",
        remote=remote,
        extra={"ref": p.get("ref") or ""},
    )


def apply_detail(job: Job, d: dict) -> None:
    sections = ((d.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        text = html_to_text(sec.get("text"))
        if text:
            title = (sec.get("title") or "").strip()
            parts.append(f"{title}\n{text}" if title else text)
    if parts:
        job.description = html_to_text("\n\n".join(parts))
    if d.get("postingUrl"):
        job.apply_url = d["postingUrl"]


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    tok = quote(token, safe="")
    jobs: list[Job] = []
    offset = 0
    while True:
        data = await get_json(fetcher, LIST.format(token=tok, limit=PAGE, offset=offset),
                              what="smartrecruiters api")
        content = data.get("content") or []
        jobs.extend(map_summary(p, token) for p in content if p.get("name"))
        total = int(data.get("totalFound") or 0)
        offset += len(content)
        if not content or offset >= total or offset >= 5000:
            break
    details = 0
    for job in jobs:
        if details >= config.MAX_DETAIL_FETCHES or known(job.external_id):
            continue
        details += 1
        url = job.extra.get("ref") or DETAIL.format(token=tok, id=quote(job.external_id, safe=""))
        try:
            apply_detail(job, await get_json(fetcher, url, what="smartrecruiters detail"))
        except AtsError as e:
            log.debug("smartrecruiters detail %s: %s", job.external_id, e)
    return jobs

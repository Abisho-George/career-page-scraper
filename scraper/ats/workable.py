"""Workable public jobs.

Primary: the public widget endpoint (one call, includes descriptions with details=true)
    GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
Fallback: the endpoint the apply.workable.com/{token} page itself loads
    POST https://apply.workable.com/api/v3/accounts/{token}/jobs   (paged with a "token")
    GET  https://apply.workable.com/api/v2/accounts/{token}/jobs/{shortcode}  (description)
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import config
from scraper.ats.base import AtsError, Job, get_json, html_to_text, join_nonempty, never_known

log = logging.getLogger(__name__)

WIDGET = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
V3_LIST = "https://apply.workable.com/api/v3/accounts/{token}/jobs"
V2_DETAIL = "https://apply.workable.com/api/v2/accounts/{token}/jobs/{shortcode}"
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on_site": "onsite", "onsite": "onsite"}


def map_widget_job(j: dict, token: str) -> Job:
    locs = [loc for loc in j.get("locations") or [] if not loc.get("hidden")]
    if locs:
        location = " / ".join(join_nonempty(loc.get("city"), loc.get("region"), loc.get("country")) for loc in locs)
    else:
        location = join_nonempty(j.get("city"), j.get("state"), j.get("country"))
    shortcode = j.get("shortcode") or ""
    return Job(
        title=(j.get("title") or "").strip(),
        apply_url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{token}/j/{shortcode}/",
        location=location,
        department=j.get("department") or "",
        employment_type=j.get("employment_type") or "",
        description=html_to_text(j.get("description")),
        posted_at=j.get("published_on") or j.get("created_at") or "",
        external_id=shortcode or str(j.get("code") or ""),
        source_method="ats_api",
        remote="remote" if j.get("telecommuting") else "",
    )


def map_v3_job(j: dict, token: str) -> Job:
    loc = j.get("location") or {}
    dept = j.get("department")
    if isinstance(dept, list):
        dept = ", ".join(str(d) for d in dept)
    shortcode = j.get("shortcode") or ""
    remote = WORKPLACE.get(str(j.get("workplace") or "").lower(), "")
    if not remote and j.get("remote"):
        remote = "remote"
    return Job(
        title=(j.get("title") or "").strip(),
        apply_url=f"https://apply.workable.com/{token}/j/{shortcode}/",
        location=join_nonempty(loc.get("city"), loc.get("region"), loc.get("country")),
        department=dept or "",
        employment_type=str(j.get("type") or "").replace("_", "-"),
        posted_at=j.get("published") or "",
        external_id=shortcode,
        source_method="ats_api",
        remote=remote,
    )


async def _v3(token: str, fetcher, known) -> list[Job]:
    tok = quote(token, safe="")
    jobs: list[Job] = []
    payload: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    for _ in range(50):
        data = await get_json(fetcher, V3_LIST.format(token=tok), method="POST", payload=payload,
                              what="workable api")
        jobs.extend(map_v3_job(j, token) for j in data.get("results") or [] if j.get("title"))
        nxt = data.get("nextPage")
        if not nxt:
            break
        payload = {**payload, "token": nxt}
    details = 0
    for job in jobs:
        if details >= config.MAX_DETAIL_FETCHES or known(job.external_id):
            continue
        details += 1
        try:
            d = await get_json(fetcher, V2_DETAIL.format(token=tok, shortcode=job.external_id),
                               what="workable detail")
            job.description = html_to_text("\n\n".join(
                x for x in (d.get("description"), d.get("requirements"), d.get("benefits")) if x))
        except AtsError as e:
            log.debug("workable detail %s: %s", job.external_id, e)
    return jobs


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    try:
        data = await get_json(fetcher, WIDGET.format(token=quote(token, safe="")), what="workable api")
        return [map_widget_job(j, token) for j in data.get("jobs") or [] if j.get("title")]
    except AtsError as e:
        log.debug("workable widget failed for %s (%s); trying v3", token, e)
    return await _v3(token, fetcher, known)

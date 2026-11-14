"""Greenhouse job board API: https://developers.greenhouse.io/job-board.html"""
from __future__ import annotations

from urllib.parse import quote

from scraper.ats.base import Job, get_json, html_to_text, DetailPlan, require_board

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _employment_type(job: dict) -> str:
    for m in job.get("metadata") or []:
        name = str(m.get("name") or "").lower()
        if any(k in name for k in ("employment type", "job type", "commitment", "time type")):
            v = m.get("value")
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            return str(v or "")
    return ""


def map_job(j: dict) -> Job:
    depts = ", ".join(d.get("name", "") for d in j.get("departments") or [] if d.get("name"))
    return Job(
        title=(j.get("title") or "").strip(),
        apply_url=j.get("absolute_url") or "",
        location=((j.get("location") or {}).get("name") or "").strip(),
        department=depts,
        employment_type=_employment_type(j),
        description=html_to_text(j.get("content")),
        posted_at=j.get("first_published") or j.get("updated_at") or "",
        external_id=str(j.get("id") or j.get("internal_job_id") or ""),
        source_method="ats_api",
    )


def parse(data: dict) -> list[Job]:
    return [map_job(j) for j in data.get("jobs") or [] if j.get("title")]


async def fetch_jobs(token: str, fetcher, plan: DetailPlan | None = None) -> list[Job]:
    data = await get_json(fetcher, API.format(token=quote(token, safe="")), what="greenhouse api")
    return parse(require_board(data, "jobs", "greenhouse api"))

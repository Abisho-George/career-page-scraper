"""Ashby public job board API: https://developers.ashbyhq.com/docs/public-job-posting-api"""
from __future__ import annotations

from urllib.parse import quote

from scraper.ats.base import Job, get_json, html_to_text, join_nonempty, never_known, require_board

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"
EMPLOYMENT = {"FullTime": "Full-time", "PartTime": "Part-time", "Intern": "Intern",
              "Contract": "Contract", "Temporary": "Temporary"}
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def map_job(j: dict) -> Job:
    secondary = [s.get("location", "") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
    remote = WORKPLACE.get(str(j.get("workplaceType") or "").lower(), "")
    if not remote and j.get("isRemote"):
        remote = "remote"
    return Job(
        title=(j.get("title") or "").strip(),
        apply_url=j.get("jobUrl") or j.get("applyUrl") or "",
        location=join_nonempty(j.get("location"), *secondary, sep=" / "),
        department=join_nonempty(j.get("department"), j.get("team"), sep=" / "),
        employment_type=EMPLOYMENT.get(j.get("employmentType") or "", j.get("employmentType") or ""),
        description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
        posted_at=j.get("publishedAt") or j.get("publishedDate") or "",
        external_id=str(j.get("id") or ""),
        source_method="ats_api",
        remote=remote,
    )


def parse(data: dict) -> list[Job]:
    return [map_job(j) for j in data.get("jobs") or []
            if j.get("title") and j.get("isListed", True) is not False]


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    data = await get_json(fetcher, API.format(token=quote(token, safe="")), what="ashby api")
    return parse(require_board(data, "jobs", "ashby api"))

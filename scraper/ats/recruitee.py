"""Recruitee careers site API: https://docs.recruitee.com/reference/offers"""
from __future__ import annotations

from scraper.ats.base import Job, get_json, html_to_text, join_nonempty, never_known

API = "https://{token}.recruitee.com/api/offers/"
EMPLOYMENT = {"fulltime": "Full-time", "parttime": "Part-time", "contract": "Contract",
              "internship": "Intern", "freelance": "Freelance", "temporary": "Temporary",
              "fulltime_permanent": "Full-time", "fulltime_fixed_term": "Full-time (fixed term)"}


def map_job(o: dict, token: str) -> Job:
    remote = ""
    if o.get("remote"):
        remote = "remote"
    elif o.get("hybrid"):
        remote = "hybrid"
    elif o.get("on_site"):
        remote = "onsite"
    desc = "\n\n".join(x for x in (html_to_text(o.get("description")), html_to_text(o.get("requirements"))) if x)
    code = o.get("employment_type_code") or ""
    return Job(
        title=(o.get("title") or "").strip(),
        apply_url=o.get("careers_url") or f"https://{token}.recruitee.com/o/{o.get('slug', '')}",
        location=o.get("location") or join_nonempty(o.get("city"), o.get("state_name"), o.get("country")),
        department=o.get("department") or "",
        employment_type=EMPLOYMENT.get(code, code.replace("_", " ")),
        description=desc,
        posted_at=o.get("published_at") or o.get("created_at") or "",
        external_id=str(o.get("id") or o.get("slug") or ""),
        source_method="ats_api",
        remote=remote,
    )


def parse(data: dict, token: str) -> list[Job]:
    return [map_job(o, token) for o in data.get("offers") or []
            if o.get("title") and (o.get("status") in (None, "published"))]


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    return parse(await get_json(fetcher, API.format(token=token.lower()), what="recruitee api"), token)

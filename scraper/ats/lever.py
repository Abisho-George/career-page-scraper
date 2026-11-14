"""Lever postings API: https://github.com/lever/postings-api"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from scraper.ats.base import AtsBoardGone, Job, get_json, html_to_text, join_nonempty, never_known

API = "https://api.lever.co/v0/postings/{token}?mode=json"
API_EU = "https://api.eu.lever.co/v0/postings/{token}?mode=json"
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def _description(p: dict) -> str:
    parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
    for lst in p.get("lists") or []:
        body = html_to_text(lst.get("content"))
        parts.append(f"{lst.get('text', '').strip()}\n{body}".strip())
    parts.append(p.get("additionalPlain") or html_to_text(p.get("additional")))
    return html_to_text("\n\n".join(x.strip() for x in parts if x and x.strip()))


def map_job(p: dict) -> Job:
    cats = p.get("categories") or {}
    location = cats.get("location") or ""
    all_locs = cats.get("allLocations") or []
    if len(all_locs) > 1:
        location = join_nonempty(location, *all_locs, sep=" / ")
    created = p.get("createdAt")
    posted = ""
    if isinstance(created, (int, float)):
        posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat()
    return Job(
        title=(p.get("text") or "").strip(),
        apply_url=p.get("hostedUrl") or p.get("applyUrl") or "",
        location=location,
        department=join_nonempty(cats.get("department"), cats.get("team"), sep=" / "),
        employment_type=cats.get("commitment") or "",
        description=_description(p),
        posted_at=posted,
        external_id=str(p.get("id") or ""),
        source_method="ats_api",
        remote=WORKPLACE.get(str(p.get("workplaceType") or "").lower(), ""),
    )


def parse(data) -> list[Job]:
    if isinstance(data, dict):  # error payloads look like {"ok": false, "error": "..."}
        if data.get("ok") is False or "data" not in data:
            raise AtsBoardGone(f"lever api: invalid board response ({data.get('error') or 'not a list'})")
        data = data["data"] or []
    if not isinstance(data, list):
        raise AtsBoardGone("lever api: invalid board response (not a list)")
    return [map_job(p) for p in data if p.get("text")]


async def fetch_jobs(token: str, fetcher, known=never_known) -> list[Job]:
    if token.startswith("eu/"):
        url = API_EU.format(token=quote(token[3:], safe=""))
    else:
        url = API.format(token=quote(token, safe=""))
    return parse(await get_json(fetcher, url, what="lever api"))

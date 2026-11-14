import asyncio
import json

import pytest

from scraper.ats import ashby, greenhouse, lever, recruitee, smartrecruiters, workable, workday
from scraper.ats.base import AtsError, BlockedError
from tests.helpers import Router, fixture_json


def run(router, mod, token, **kw):
    async def go():
        async with router.fetcher() as f:
            return await mod.fetch_jobs(token, f, **kw)
    return asyncio.run(go())


def test_greenhouse():
    r = Router({"https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": (200, fixture_json("greenhouse_jobs.json"))})
    jobs = run(r, greenhouse, "acme")
    assert len(jobs) == 2
    j = jobs[0]
    assert j.title == "Senior Backend Engineer"
    assert j.apply_url == "https://job-boards.greenhouse.io/acme/jobs/4012345007"
    assert j.location == "Bangalore, India" and j.department == "Engineering"
    assert j.employment_type == "Full-time" and j.external_id == "4012345007"
    assert "<" not in j.description and "Senior Backend Engineer" in j.description and "Python" in j.description
    assert j.posted_at.startswith("2026-10-20") and j.source_method == "ats_api"


def test_greenhouse_bad_token_is_ats_error():
    with pytest.raises(AtsError):
        run(Router(), greenhouse, "nope")


def test_ats_403_is_blocked():
    r = Router({"https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": (403, "denied")})
    with pytest.raises(BlockedError):
        run(r, greenhouse, "acme")


def test_lever():
    r = Router({"https://api.lever.co/v0/postings/acme?mode=json": (200, fixture_json("lever_postings.json"))})
    jobs = run(r, lever, "acme")
    assert [j.title for j in jobs] == ["Platform Engineer", "Design Intern"]
    j = jobs[0]
    assert j.apply_url.startswith("https://jobs.lever.co/acme/5a1b")
    assert j.location == "Mumbai / Pune" and j.department == "Engineering / Platform"
    assert j.employment_type == "Full-time" and j.remote == "hybrid"
    assert "Requirements" in j.description and "Kubernetes" in j.description and "benefits" in j.description
    assert j.posted_at == "2025-10-31"
    assert jobs[1].remote == "remote"


def test_lever_eu():
    r = Router({"https://api.eu.lever.co/v0/postings/acme?mode=json": (200, [])})
    assert run(r, lever, "eu/acme") == []


def test_ashby():
    r = Router({"https://api.ashbyhq.com/posting-api/job-board/acme": (200, fixture_json("ashby_jobs.json"))})
    jobs = run(r, ashby, "acme")
    assert len(jobs) == 1  # unlisted job skipped
    j = jobs[0]
    assert j.title == "Data Scientist" and j.location == "Gurgaon / Bangalore"
    assert j.employment_type == "Full-time" and j.remote == "onsite" and j.department == "Data / ML"


def test_smartrecruiters_pagination_and_detail():
    base = "https://api.smartrecruiters.com/v1/companies/Acme/postings"
    r = Router({
        f"{base}?limit=100&offset=0": (200, fixture_json("smartrecruiters_page1.json")),
        f"{base}?limit=100&offset=1": (200, fixture_json("smartrecruiters_page2.json")),
        f"{base}/744000012345678": (200, fixture_json("smartrecruiters_detail.json")),
    })
    jobs = run(r, smartrecruiters, "Acme")
    assert [j.title for j in jobs] == ["Store Manager", "Remote Support Agent"]
    assert jobs[0].location == "Chennai, TN, India" and jobs[0].department == "Retail"
    assert "Run the store." in jobs[0].description and "5 years retail" in jobs[0].description
    assert jobs[0].apply_url.endswith("744000012345678-store-manager")
    assert jobs[1].remote == "remote" and jobs[1].description == ""  # detail 404 -> kept without description


def test_smartrecruiters_skips_known_details():
    base = "https://api.smartrecruiters.com/v1/companies/Acme/postings"
    r = Router({f"{base}?limit=100&offset=0": (200, {**fixture_json("smartrecruiters_page1.json"), "totalFound": 1})})
    run(r, smartrecruiters, "Acme", known=lambda ext: True)
    assert not any("/postings/7440" in u for u in r.urls())


def test_workable_widget():
    r = Router({"https://apply.workable.com/api/v1/widget/accounts/acme?details=true": (200, fixture_json("workable_widget.json"))})
    jobs = run(r, workable, "acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Customer Success Manager" and j.location == "Hyderabad, Telangana, India"
    assert j.apply_url == "https://apply.workable.com/j/A1B2C3D4E5" and j.description == "Keep customers happy."


def test_workable_v3_fallback():
    def v3(req):
        body = json.loads(req.content)
        assert body["query"] == ""
        return (200, fixture_json("workable_v3.json"))
    r = Router({
        "POST https://apply.workable.com/api/v3/accounts/acme/jobs": v3,
        "https://apply.workable.com/api/v2/accounts/acme/jobs/ZZZZ000001": (200, fixture_json("workable_v2_detail.json")),
    })
    jobs = run(r, workable, "acme")
    j = jobs[0]
    assert j.title == "Backend Developer" and j.remote == "hybrid" and j.department == "Engineering"
    assert "Write APIs." in j.description and "Node.js" in j.description
    assert j.apply_url == "https://apply.workable.com/acme/j/ZZZZ000001/"


def test_recruitee():
    r = Router({"https://acme.recruitee.com/api/offers/": (200, fixture_json("recruitee_offers.json"))})
    j = run(r, recruitee, "acme")[0]
    assert j.title == "Frontend Developer" and j.remote == "hybrid" and j.employment_type == "Full-time"
    assert "Build UI." in j.description and "React" in j.description


def test_workday_paginates_to_total_and_fetches_details():
    cxs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    offsets = []

    def listing(req):
        body = json.loads(req.content)
        offsets.append(body["offset"])
        assert body["limit"] == 20 and body["appliedFacets"] == {}
        return (200, fixture_json("workday_page1.json" if body["offset"] == 0 else "workday_page2.json"))

    routes = {f"POST {cxs}/jobs": listing,
              f"{cxs}/job/Bengaluru/Analyst-0_R1000": (200, fixture_json("workday_detail.json"))}
    r = Router(routes)
    jobs = run(r, workday, "acme|wd5|External")
    assert offsets == [0, 20]
    assert len(jobs) == 25  # full count even though page 2 reports total 0
    j = jobs[0]
    assert j.external_id == "R1000" and "Analyse data." in j.description
    assert j.location == "Bengaluru / Hyderabad" and j.remote == "hybrid" and j.posted_at == "2026-11-11"
    assert j.apply_url.endswith("/External/job/Bengaluru/Analyst-0_R1000")
    assert jobs[5].apply_url == "https://acme.wd5.myworkdayjobs.com/External/job/Bengaluru/Analyst-5_R1005"


def test_workday_detail_cap(monkeypatch):
    import config
    monkeypatch.setattr(config, "MAX_DETAIL_FETCHES", 3)
    cxs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    r = Router({f"POST {cxs}/jobs": (200, fixture_json("workday_page1.json") | {"total": 20})})
    run(r, workday, "acme|wd5|External")
    assert sum(1 for m, u in r.calls if m == "GET" and "/job/" in u) == 3

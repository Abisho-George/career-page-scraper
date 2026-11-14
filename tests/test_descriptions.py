"""Detail-call budget: new jobs first, then stored jobs still missing a description (oldest first)."""
import asyncio
import json
import re

import config
from scraper import db, diff
from scraper.ats import smartrecruiters
from scraper.ats.base import DetailPlan, Job
from scraper.pipeline import Context, process_company
from tests.helpers import Router, fixture_json


def jobs(*ids):
    return [Job(title=i, apply_url=f"https://x.com/{i}", external_id=i) for i in ids]


def test_plan_orders_new_then_oldest_empty_and_respects_limit():
    plan = DetailPlan(stored_full={"done"},
                      stored_empty={"old2": "2026-11-02", "old1": "2026-11-01", "old3": "2026-11-03"},
                      limit=4)
    listing = jobs("old3", "new1", "done", "old1", "new2", "old2", "new1")
    assert [j.external_id for j in plan.pick(listing)] == ["new1", "new2", "old1", "old2"]
    assert DetailPlan(limit=0).pick(listing) == []


def test_plan_from_database_uses_first_seen(conn):
    cid = db.upsert_company(conn, "X", "x.com")
    for ext, desc, seen in [("a", "", "2026-11-03"), ("b", "", "2026-11-01"), ("c", "full", "2026-10-01")]:
        db.upsert_job(conn, cid, diff.make_job_id("x.com", ext), {"title": ext, "apply_url": ext, "description": desc}, seen)
    picked = diff.detail_plan(conn, cid, "x.com").pick(jobs("a", "b", "c", "d"))
    assert [j.external_id for j in picked] == ["d", "b", "a"]


def test_smartrecruiters_spends_budget_on_new_first(monkeypatch):
    monkeypatch.setattr(config, "MAX_DETAIL_FETCHES", 1)
    base = "https://api.smartrecruiters.com/v1/companies/Acme/postings"
    page = {"offset": 0, "limit": 100, "totalFound": 2, "content": [
        {**fixture_json("smartrecruiters_page1.json")["content"][0], "ref": ""},
        {**fixture_json("smartrecruiters_page2.json")["content"][0], "ref": ""}]}
    r = Router({f"{base}?limit=100&offset=0": (200, page)})
    plan = DetailPlan(job_key=lambda j: j.external_id, stored_empty={"744000012345678": "2026-01-01"})

    async def go():
        async with r.fetcher() as f:
            return await smartrecruiters.fetch_jobs("Acme", f, plan)
    asyncio.run(go())
    detail_calls = [u for m, u in r.calls if re.search(r"/postings/\d+$", u)]
    assert detail_calls == [f"{base}/744000099999999"]  # the new job, not the stored one


# ---------------------------------------------------------------- 500-job Workday company

CXS = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
N = 500


def workday_routes(n=N, failing=()):
    def listing(req):
        offset = json.loads(req.content)["offset"]
        postings = [{"title": f"Role {i}", "externalPath": f"/job/Pune/Role-{i}_R{i}", "locationsText": "Pune",
                     "postedOn": "Posted Today", "bulletFields": [f"R{i}"]} for i in range(offset, min(offset + 20, n))]
        return (200, {"total": n if offset == 0 else 0, "jobPostings": postings})

    def detail(req):
        i = int(re.search(r"_R(\d+)$", req.url.path).group(1))
        if i in failing:
            return (500, "oops")
        return (200, {"jobPostingInfo": {"title": f"Role {i}", "jobDescription": f"<p>Description {i}</p>"}})

    routes = {f"POST {CXS}/jobs": listing}
    routes.update({f"{CXS}/job/Pune/Role-{i}_R{i}": detail for i in range(n)})
    return routes


def run_workday(conn, routes):
    router = Router(routes)

    async def go():
        run_id, _ = db.start_run(conn, fresh=True)
        async with router.fetcher() as f:
            await process_company(Context(conn, f, None, run_id), db.get_company(conn, "acme.com"))
    asyncio.run(go())
    return router


def missing(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE description = ''").fetchone()[0]


def setup_company(conn):
    cid = db.upsert_company(conn, "Acme", "acme.com")
    db.update_company(conn, cid, ats="workday", ats_token="acme|wd5|External")
    conn.commit()


def test_500_jobs_all_described_after_three_runs(conn):
    assert config.MAX_DETAIL_FETCHES == 200
    setup_company(conn)
    remaining = []
    for _ in range(3):
        router = run_workday(conn, workday_routes())
        assert sum(1 for m, u in router.calls if m == "GET" and "/job/" in u) <= 200
        remaining.append(missing(conn))
    assert remaining == [300, 100, 0]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0] == N
    assert conn.execute("SELECT description FROM jobs WHERE title = 'Role 499'").fetchone()[0] == "Description 499"


def test_new_jobs_jump_the_backlog_and_failures_retry(conn):
    setup_company(conn)
    run_workday(conn, workday_routes(n=300, failing={3}))
    assert missing(conn) == 101  # 200 calls, one failed (Role 3)
    # next run: 5 new postings appear; they are described first, then the oldest backlog
    router = run_workday(conn, workday_routes(n=305))
    detail_paths = [u.rsplit("_R", 1)[1] for m, u in router.calls if m == "GET" and "/job/" in u]
    assert detail_paths[:5] == ["300", "301", "302", "303", "304"]
    assert "3" in detail_paths  # the failed one is retried
    assert missing(conn) == 0

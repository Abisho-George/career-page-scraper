import asyncio

import pytest

import config
import main
from scraper import db, pipeline
from scraper.fetch import Response
from scraper.pipeline import Context, process_company
from tests.helpers import Router, fixture, fixture_json

GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
CF = (403, fixture("cloudflare_challenge.html"))

ROUTES = {
    # acme.com: careers page embeds Greenhouse
    "https://acme.com/careers": (200, fixture("careers_greenhouse_embed.html")),
    GH_API: (200, fixture_json("greenhouse_jobs.json")),
    # bigbrand.com: Cloudflare everywhere
    **{f"https://bigbrand.com{p}": CF for p in config.CAREERS_PATHS + ["/"]},
    # custom.com: plain HTML careers page, job detail pages
    "https://custom.com/careers": (200, fixture("normal_careers.html").replace("acme.com", "custom.com")),
    "https://custom.com/careers/senior-backend-engineer-1234": (200, fixture("job_detail_1.html")),
    "https://custom.com/careers/enterprise-account-executive": (200, fixture("job_detail_2.html")),
    "https://custom.com/careers/product-designer-ii": (200, fixture("job_detail_3.html")),
    # globex.com: JSON-LD listing
    "https://globex.com/jobs": (200, fixture("jsonld_listing.html")),
    # tata.com: Darwinbox (detected, not extracted)
    "https://tata.com/careers": (200, fixture("careers_darwinbox.html")),
    # nothing.com: homepage with no careers link
    "https://nothing.com/": (200, "<html><head><title>Nothing</title></head><body><a href='/about'>About</a></body></html>"),
}


def make_ctx(conn, router, browser=None):
    run_id, _ = db.start_run(conn)
    return Context(conn=conn, fetcher=router.fetcher(), browser=browser, run_id=run_id)


def process(conn, router, domain, browser=None, careers_url=""):
    db.upsert_company(conn, domain, domain, careers_url)
    conn.commit()

    async def go():
        ctx = make_ctx(conn, router, browser)
        try:
            return await process_company(ctx, db.get_company(conn, domain))
        finally:
            await ctx.fetcher.close()
    return asyncio.run(go())


def company(conn, domain):
    return db.get_company(conn, domain)


def test_greenhouse_company_then_fast_path(conn):
    r = Router(ROUTES)
    out = process(conn, r, "acme.com")
    c = company(conn, "acme.com")
    assert (c["status"], c["method"], c["ats"], c["ats_token"], c["job_count"]) == ("OK", "ats_api", "greenhouse", "acme", 2)
    assert c["careers_url"] == "https://acme.com/careers" and c["careers_url_source"] == "discovered"
    loc = conn.execute("SELECT location FROM jobs WHERE title = 'Senior Backend Engineer'").fetchone()[0]
    assert loc == "Bengaluru, India"  # normalized
    assert out.status == "OK"
    # second run goes straight to the API
    r2 = Router(ROUTES)
    process(conn, r2, "acme.com")
    assert r2.urls() == ["https://boards-api.greenhouse.io/robots.txt", GH_API]


def test_blocked(conn):
    process(conn, Router(ROUTES), "bigbrand.com")
    c = company(conn, "bigbrand.com")
    assert (c["status"], c["status_reason"]) == ("BLOCKED", "cloudflare challenge")


def test_sheet_url_that_is_blocked(conn):
    process(conn, Router(ROUTES), "bigbrand.com", careers_url="https://bigbrand.com/careers")
    assert company(conn, "bigbrand.com")["status"] == "BLOCKED"


ROBOTS_ALL = (200, "User-agent: *\nDisallow: /\n", {"content-type": "text/plain"})


def test_robots_disallow_on_careers_page(conn):
    routes = {**ROUTES, "https://acme.com/robots.txt": ROBOTS_ALL}
    process(conn, Router(routes), "acme.com", careers_url="https://acme.com/careers")
    c = company(conn, "acme.com")
    assert (c["status"], c["status_reason"]) == ("ROBOTS_DISALLOWED", "robots.txt disallows /careers")


def test_robots_disallow_on_ats_endpoint(conn):
    process(conn, Router(ROUTES), "acme.com")  # learns the greenhouse token
    routes = {**ROUTES, "https://boards-api.greenhouse.io/robots.txt": ROBOTS_ALL}
    process(conn, Router(routes), "acme.com")
    c = company(conn, "acme.com")
    assert (c["status"], c["status_reason"]) == ("ROBOTS_DISALLOWED", "robots.txt disallows /v1/boards/acme/jobs")
    assert c["job_count"] == 2  # nothing closed


def test_robots_disallow_during_discovery(conn):
    process(conn, Router({"https://acme.com/robots.txt": ROBOTS_ALL}), "acme.com")
    c = company(conn, "acme.com")
    assert (c["status"], c["status_reason"]) == ("ROBOTS_DISALLOWED", "robots.txt disallows /careers")


def test_bot_protection_wins_over_robots_in_discovery(conn):
    routes = {**{k: v for k, v in ROUTES.items() if k.startswith("https://bigbrand.com")},
              "https://bigbrand.com/robots.txt": (200, "User-agent: *\nDisallow: /careers\n", {"content-type": "text/plain"})}
    process(conn, Router(routes), "bigbrand.com")
    c = company(conn, "bigbrand.com")
    assert (c["status"], c["status_reason"]) == ("BLOCKED", "cloudflare challenge")


def test_custom_page_heuristic(conn):
    process(conn, Router(ROUTES), "custom.com")
    c = company(conn, "custom.com")
    assert (c["status"], c["method"], c["ats"], c["job_count"]) == ("OK", "heuristic", "custom", 3)
    urls = {r[0] for r in conn.execute("SELECT apply_url FROM jobs")}
    assert "https://custom.com/careers/product-designer-ii" in urls


def test_jsonld_page(conn):
    process(conn, Router(ROUTES), "globex.com")
    c = company(conn, "globex.com")
    assert (c["status"], c["method"], c["job_count"]) == ("OK", "jsonld", 2)
    assert conn.execute("SELECT location FROM jobs WHERE title='Field Sales Officer'").fetchone()[0] == "Gurugram, India / Noida, India"


def test_flag_only_ats(conn):
    process(conn, Router(ROUTES), "tata.com")
    c = company(conn, "tata.com")
    assert (c["status"], c["ats"], c["status_reason"]) == ("NO_JOBS_FOUND", "darwinbox", "darwinbox detected; no extractor yet")


def test_no_careers_page(conn):
    process(conn, Router(ROUTES), "nothing.com")
    c = company(conn, "nothing.com")
    assert (c["status"], c["status_reason"]) == ("NO_CAREERS_PAGE", "no careers link on homepage")


def test_sheet_url_404(conn):
    process(conn, Router(ROUTES), "acme.com", careers_url="https://acme.com/old-careers")
    c = company(conn, "acme.com")
    assert c["status"] == "NO_CAREERS_PAGE" and "404" in c["status_reason"]


def test_stored_url_404_rediscovers(conn):
    cid = db.upsert_company(conn, "custom.com", "custom.com")
    db.update_company(conn, cid, careers_url="https://custom.com/old", careers_url_source="discovered")
    conn.commit()
    process(conn, Router(ROUTES), "custom.com")
    c = company(conn, "custom.com")
    assert c["status"] == "OK" and c["careers_url"] == "https://custom.com/careers"


def test_exception_becomes_error_and_keeps_jobs(conn, monkeypatch):
    process(conn, Router(ROUTES), "acme.com")
    db.update_company(conn, company(conn, "acme.com")["id"], ats=None, ats_token=None)
    conn.commit()

    def boom(*a, **k):
        raise ValueError("parser exploded")
    monkeypatch.setattr(pipeline, "detect_ats", boom)
    out = process(conn, Router(ROUTES), "acme.com")
    c = company(conn, "acme.com")
    assert out.status == "ERROR" and c["status_reason"].startswith("ValueError")
    assert c["job_count"] == 2 and c["careers_url"] == "https://acme.com/careers"  # nothing closed or forgotten


def test_timeout(conn, monkeypatch):
    monkeypatch.setattr(config, "COMPANY_TIMEOUT", 0.3)

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(pipeline, "discover", slow)
    out = process(conn, Router(ROUTES), "slow.com")
    assert out.status == "ERROR" and out.reason.startswith("timeout")


class FakeBrowser:
    def __init__(self, html):
        self.html, self.calls = html, []

    async def render(self, url):
        self.calls.append(url)
        return Response(url=url, status=200, headers={}, text=self.html)


def test_browser_fallback_for_spa(conn):
    rendered = fixture("normal_careers.html").replace("acme.com", "spa.com").replace("/careers/", "/careers/")
    routes = {
        "https://spa.com/careers": (200, fixture("spa_shell.html")),
        "https://spa.com/careers/senior-backend-engineer-1234": (200, fixture("job_detail_1.html")),
        "https://spa.com/careers/enterprise-account-executive": (200, fixture("job_detail_2.html")),
        "https://spa.com/careers/product-designer-ii": (200, fixture("job_detail_3.html")),
    }
    browser = FakeBrowser(rendered)
    process(conn, Router(routes), "spa.com", browser=browser)
    c = company(conn, "spa.com")
    assert browser.calls == ["https://spa.com/careers"]
    assert (c["status"], c["method"], c["job_count"]) == ("OK", "browser", 3)


def test_browser_finds_ats_iframe(conn):
    routes = {"https://spa.com/careers": (200, fixture("spa_shell.html")), GH_API: (200, fixture_json("greenhouse_jobs.json"))}
    browser = FakeBrowser('<html><body><iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe></body></html>')
    process(conn, Router(routes), "spa.com", browser=browser)
    c = company(conn, "spa.com")
    assert (c["status"], c["method"], c["ats"]) == ("OK", "ats_api", "greenhouse")


def test_rendered_page_blocked(conn):
    routes = {"https://spa.com/careers": (200, fixture("spa_shell.html"))}
    process(conn, Router(routes), "spa.com", browser=FakeBrowser(fixture("cloudflare_challenge.html")))
    assert company(conn, "spa.com")["status_reason"] == "cloudflare challenge (rendered)"


def test_browser_not_used_for_normal_pages(conn):
    browser = FakeBrowser("")
    process(conn, Router(ROUTES), "nothing.com", browser=browser)
    process(conn, Router(ROUTES), "tata.com", browser=browser)
    assert browser.calls == []


# ---------------------------------------------------------------- full runs through main.py

@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "MAX_PARALLEL_COMPANIES", 1)
    monkeypatch.setattr(config, "USE_BROWSER", False)
    csv = tmp_path / "companies.csv"
    csv.write_text("company_name,website,careers_url\nAcme,acme.com,\nBig Brand,bigbrand.com,\n"
                   "Custom,https://www.custom.com/,\nGlobex,globex.com,\nTata,tata.com,\nNothing,nothing.com,\n")
    routers = []

    def fake_fetcher(*a, **k):
        r = Router(ROUTES)
        routers.append(r)
        return r.fetcher()
    monkeypatch.setattr(main, "Fetcher", fake_fetcher)
    return csv, routers


def statuses():
    conn = db.connect()
    return {r["website"]: r["status"] for r in conn.execute("SELECT * FROM companies")}


def test_full_run_twice_is_stable(cli):
    csv, routers = cli
    assert main.main(["run", "--csv", str(csv), "--no-sheets"]) == 0
    assert statuses() == {"acme.com": "OK", "bigbrand.com": "BLOCKED", "custom.com": "OK", "globex.com": "OK",
                          "tata.com": "NO_JOBS_FOUND", "nothing.com": "NO_CAREERS_PAGE"}
    conn = db.connect()
    run1 = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert (run1["jobs_new"], run1["jobs_closed"], run1["ok"]) == (7, 0, 3)
    assert main.main(["run", "--csv", str(csv), "--no-sheets"]) == 0
    run2 = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert run2["id"] != run1["id"] and (run2["jobs_new"], run2["jobs_closed"]) == (0, 0)
    tabs = main.build_tabs(conn)
    assert len(tabs["Jobs"]) == 8 and tabs["Company Status"][1][2] == "BLOCKED"  # problems first
    assert "robots_disallowed" in tabs["Runs"][0]
    assert len(tabs["Runs"]) == 3 and tabs["Runs"][1][0] == run2["id"]  # newest first


def test_killed_run_resumes_remaining_only(cli, monkeypatch):
    csv, routers = cli
    real = pipeline.process_company
    done = []

    async def dies_on_third(ctx, company):
        if len(done) == 2:
            raise KeyboardInterrupt  # simulate the process being killed mid-run
        done.append(company["website"])
        return await real(ctx, company)
    monkeypatch.setattr(main, "process_company", dies_on_third)
    with pytest.raises(KeyboardInterrupt):
        main.main(["run", "--csv", str(csv), "--no-sheets"])
    first_two = list(done)

    monkeypatch.setattr(main, "process_company", real)
    seen = []

    async def record(ctx, company):
        seen.append(company["website"])
        return await real(ctx, company)
    monkeypatch.setattr(main, "process_company", record)
    assert main.main(["run", "--csv", str(csv), "--no-sheets"]) == 0
    assert len(seen) == 4 and not set(seen) & set(first_two)
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1  # same run, now finished
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 7  # no duplicates


def test_limit(cli):
    csv, _ = cli
    main.main(["run", "--csv", str(csv), "--no-sheets", "--limit", "2"])
    assert sum(1 for s in statuses().values() if s != "PENDING") == 2


def test_company_command(cli, capsys):
    assert main.main(["company", "https://www.acme.com"]) == 0
    out = capsys.readouterr().out
    assert "acme.com: OK" in out and "Senior Backend Engineer" in out


def test_ats_careers_url_goes_straight_to_api(conn):
    lever_api = "https://api.lever.co/v0/postings/hooli?mode=json"
    routes = {"https://hooli.com/": (200, fixture("homepage_with_ats_link.html")),
              lever_api: (200, fixture_json("lever_postings.json"))}
    r = Router(routes)
    process(conn, r, "hooli.com")
    c = company(conn, "hooli.com")
    assert (c["status"], c["ats"], c["ats_token"], c["job_count"]) == ("OK", "lever", "hooli", 2)
    assert c["careers_url"] == "https://jobs.lever.co/hooli"
    assert not any(u.startswith("https://jobs.lever.co/") for u in r.urls())  # board page never fetched


def test_robots_company_counted_and_sorted(cli, capsys, monkeypatch):
    csv, _ = cli
    routes = {**ROUTES, "https://nothing.com/robots.txt": ROBOTS_ALL}
    monkeypatch.setattr(main, "Fetcher", lambda *a, **k: Router(routes).fetcher())
    m = main
    assert m.main(["run", "--csv", str(csv), "--no-sheets"]) == 0
    assert statuses()["nothing.com"] == "ROBOTS_DISALLOWED"
    out = capsys.readouterr().out
    assert "BLOCKED 1 | ROBOTS_DISALLOWED 1 |" in out
    conn = db.connect()
    order = [row[2] for row in m.build_tabs(conn)["Company Status"][1:]]
    assert order[:2] == ["BLOCKED", "ROBOTS_DISALLOWED"]
    assert conn.execute("SELECT robots_disallowed FROM runs").fetchone()[0] == 1

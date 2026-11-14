import asyncio

from scraper.discover import discover, find_careers_url
from tests.helpers import Router, fixture

CAREERS = (200, fixture("normal_careers.html"))


def run(router, domain, **kw):
    async def go():
        async with router.fetcher() as f:
            return await discover(domain, f, **kw)
    return asyncio.run(go())


def test_sheet_url_wins():
    d = run(Router(), "acme.com", sheet_url="https://acme.com/x")
    assert d.url == "https://acme.com/x" and d.reason == "from sheet"


def test_probe_finds_careers_path():
    r = Router({"https://acme.com/careers": CAREERS})
    d = run(r, "acme.com")
    assert d.url == "https://acme.com/careers" and d.page is not None


def test_soft_404_and_homepage_redirect_are_rejected():
    r = Router({
        "https://acme.com/careers": (200, fixture("soft_404.html")),
        "https://acme.com/jobs": (302, "", {"location": "https://acme.com/"}),
        "https://acme.com/": (200, fixture("homepage_with_careers_link.html")),
        "https://acme.com/company/work-at-initech": CAREERS,
    })
    d = run(r, "acme.com")
    assert d.url == "https://acme.com/company/work-at-initech"
    assert d.reason == "homepage link"


def test_careers_subdomain():
    r = Router({"https://careers.acme.com/": (200, "<html><head><title>Acme</title></head><body>"
                                                 "<a href='/x'>x</a></body></html>")})
    assert run(r, "acme.com").url == "https://careers.acme.com/"


def test_homepage_links_straight_to_ats():
    r = Router({"https://hooli.com/": (200, fixture("homepage_with_ats_link.html"))})
    assert run(r, "hooli.com").url == "https://jobs.lever.co/hooli"


def test_linkedin_jobs_link_is_not_a_careers_page():
    html = "<html><body><a href='https://www.linkedin.com/company/x/jobs'>Jobs</a></body></html>"
    d = run(Router({"https://x.com/": (200, html)}), "x.com")
    assert d.url is None and d.reason == "no careers link on homepage"


def test_blocked_everywhere():
    cf = (403, fixture("cloudflare_challenge.html"))
    r = Router({f"https://big.com{p}": cf for p in ("/careers", "/careers/", "/jobs", "/join-us", "/work-with-us",
                                                     "/about/careers", "/company/careers", "/")})
    d = run(r, "big.com")
    assert d.url is None and d.blocked == "cloudflare challenge"


def test_apex_unreachable_switches_to_www():
    import httpx

    def dead(req):
        return httpx.ConnectError("dns")
    routes = {f"https://acme.com{p}": dead for p in ("/careers", "/careers/", "/jobs", "/robots.txt", "/")}
    routes["https://www.acme.com/careers"] = CAREERS
    d = run(Router(routes), "acme.com")
    assert d.url == "https://www.acme.com/careers"


def test_find_careers_url_tuple():
    async def go():
        async with Router().fetcher() as f:
            return await find_careers_url("nothing.com", f)
    url, reason = asyncio.run(go())
    assert url is None and reason

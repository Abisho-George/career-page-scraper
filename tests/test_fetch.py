import asyncio

import httpx
import pytest

from scraper.ats.base import BlockedError, RobotsDisallowedError
from tests.helpers import Router

ROBOTS = "User-agent: *\nDisallow: /private\n\nUser-agent: JobScraperBot\nDisallow: /nobots\n"


def run(coro):
    return asyncio.run(coro)


def test_user_agent_and_redirects():
    seen = {}

    def page(req):
        seen["ua"] = req.headers["user-agent"]
        return (200, "<html>ok</html>")

    r = Router({"https://a.com/old": (301, "", {"location": "https://a.com/new"}), "https://a.com/new": page})

    async def go():
        async with r.fetcher() as f:
            return await f.get("https://a.com/old")

    resp = run(go())
    assert resp.status == 200 and resp.url == "https://a.com/new"
    assert seen["ua"].startswith("JobScraperBot/0.1 (+mailto:")


def test_robots_disallow_raises_blocked():
    r = Router({"https://a.com/robots.txt": (200, ROBOTS, {"content-type": "text/plain"}),
                "https://star.com/robots.txt": (200, "User-agent: *\nDisallow: /private\n", {"content-type": "text/plain"}),
                "https://a.com/nobots/x": (200, "secret")})

    async def go():
        async with r.fetcher() as f:
            with pytest.raises(RobotsDisallowedError) as e:
                await f.get("https://a.com/nobots/x")
            assert e.value.reason == "robots.txt disallows /nobots/x"
            assert e.value.status == "ROBOTS_DISALLOWED" and isinstance(e.value, BlockedError)
            # our own group overrides "*", so /private is allowed for us here
            await f.get("https://a.com/private/y")
            with pytest.raises(BlockedError):
                await f.get("https://star.com/private/y")
            ok = await f.get("https://a.com/careers")
            return ok

    assert run(go()).status == 404
    assert r.urls().count("https://a.com/robots.txt") == 1  # cached
    assert "https://a.com/nobots/x" not in r.urls()


def test_robots_5xx_disallows_all_4xx_allows_all():
    r = Router({"https://down.com/robots.txt": (500, "err"), "https://gone.com/robots.txt": (403, "no")})

    async def go():
        async with r.fetcher() as f:
            assert not await f.allowed("https://down.com/careers")
            assert await f.allowed("https://gone.com/careers")

    run(go())


def test_retries_only_on_connection_errors():
    attempts = {"n": 0}

    def flaky(req):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.ConnectError("boom")
        return (200, "ok")

    r = Router({"https://a.com/p": flaky, "https://b.com/p": (503, "busy")})

    async def go():
        async with r.fetcher() as f:
            ok = await f.get("https://a.com/p")
            busy = await f.get("https://b.com/p")
            return ok, busy

    ok, busy = run(go())
    assert ok.status == 200 and attempts["n"] == 3
    assert busy.status == 503 and r.urls().count("https://b.com/p") == 1  # never retried


def test_gives_up_after_two_retries():
    r = Router({"https://a.com/p": lambda req: httpx.ConnectTimeout("slow")})

    async def go():
        async with r.fetcher() as f:
            with pytest.raises(httpx.ConnectTimeout):
                await f.get("https://a.com/p")

    run(go())
    assert r.urls().count("https://a.com/p") == 3


def test_per_host_delay_and_no_concurrency():
    r = Router({f"https://a.com/{i}": (200, "x") for i in range(3)} | {"https://b.com/0": (200, "x")})

    async def go():
        async with r.fetcher(min_delay=0.2, max_delay=0.25, respect_robots=False) as f:
            await asyncio.gather(*(f.get(f"https://a.com/{i}") for i in range(3)), f.get("https://b.com/0"))
            return f.request_log

    log = run(go())
    a_times = sorted(t for h, t in log if h == "a.com")
    gaps = [b - a for a, b in zip(a_times, a_times[1:])]
    assert len(a_times) == 3 and all(g >= 0.19 for g in gaps)
    b_time = [t for h, t in log if h == "b.com"][0]
    assert b_time - a_times[0] < 0.15  # other hosts are not held up

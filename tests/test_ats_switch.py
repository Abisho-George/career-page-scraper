"""A company whose stored ATS board disappears, goes empty, or is replaced by another ATS."""
import asyncio

import pytest

from scraper import db
from scraper.ats import ashby, greenhouse, lever
from scraper.ats.base import AtsBoardGone, AtsError
from scraper.pipeline import ATS_SWITCH_NOTE, Context, process_company
from tests.helpers import Router, fixture, fixture_json

GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
LEVER_API = "https://api.lever.co/v0/postings/acme?mode=json"
CAREERS = "https://acme.com/careers"

GH_JOBS = (200, fixture_json("greenhouse_jobs.json"))
GH_EMPTY = (200, fixture_json("greenhouse_empty.json"))
GH_404 = (404, fixture_json("greenhouse_404.json"))
EMBED_PAGE = (200, fixture("careers_greenhouse_embed.html"))
LEVER_PAGE = (200, fixture("careers_lever_link.html"))
LEVER_JOBS = (200, fixture_json("lever_postings.json"))


def run_once(conn, routes):
    router = Router(routes)

    async def go():
        run_id, _ = db.start_run(conn, fresh=True)
        async with router.fetcher() as f:
            out = await process_company(Context(conn, f, None, run_id), db.get_company(conn, "acme.com"))
        db.finish_run(conn, run_id)
        return out
    out = asyncio.run(go())
    return out, db.get_company(conn, "acme.com"), router


def active(conn, ats_prefix=""):
    rows = conn.execute("SELECT apply_url FROM jobs WHERE is_active = 1").fetchall()
    return sorted(r[0] for r in rows if ats_prefix in r[0])


@pytest.fixture
def acme_on_greenhouse(conn):
    db.upsert_company(conn, "Acme", "acme.com")
    conn.commit()
    out, c, _ = run_once(conn, {CAREERS: EMBED_PAGE, GH_API: GH_JOBS})
    assert (c["status"], c["ats"], c["ats_token"], c["job_count"]) == ("OK", "greenhouse", "acme", 2)
    return conn


# ---------------------------------------------------------------- module level

def test_404_board_is_board_gone():
    async def go():
        async with Router({GH_API: GH_404, LEVER_API: (404, fixture_json("lever_404.json"))}).fetcher() as f:
            for mod in (greenhouse, lever):
                with pytest.raises(AtsBoardGone):
                    await mod.fetch_jobs("acme", f)
    asyncio.run(go())


def test_invalid_board_payload_is_board_gone():
    async def go():
        r = Router({"https://api.ashbyhq.com/posting-api/job-board/acme": (200, fixture_json("ashby_invalid_board.json")),
                    LEVER_API: (200, fixture_json("lever_404.json"))})
        async with r.fetcher() as f:
            with pytest.raises(AtsBoardGone):
                await ashby.fetch_jobs("acme", f)
            with pytest.raises(AtsBoardGone):
                await lever.fetch_jobs("acme", f)
    asyncio.run(go())


def test_server_error_is_not_board_gone():
    async def go():
        async with Router({GH_API: (500, "oops")}).fetcher() as f:
            with pytest.raises(AtsError) as e:
                await greenhouse.fetch_jobs("acme", f)
            assert not isinstance(e.value, AtsBoardGone)
    asyncio.run(go())


# ---------------------------------------------------------------- pipeline

def test_404_board_rediscovers_new_ats_same_run(acme_on_greenhouse):
    conn = acme_on_greenhouse
    out, c, router = run_once(conn, {GH_API: GH_404, CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"], c["ats_token"], c["method"]) == ("OK", "lever", "acme", "ats_api")
    assert c["status_reason"] == ATS_SWITCH_NOTE
    # never close jobs on the run where the ATS call failed: old greenhouse jobs still open
    assert len(active(conn, "greenhouse")) == 2 and len(active(conn, "lever")) == 2
    assert CAREERS in router.urls()  # discovery ran again in the same run
    # next run uses the new ATS directly and closes the old jobs
    out, c, router = run_once(conn, {LEVER_API: LEVER_JOBS})
    assert router.urls() == ["https://api.lever.co/robots.txt", LEVER_API]
    assert c["status_reason"] == "" and active(conn, "greenhouse") == [] and len(active(conn, "lever")) == 2
    assert conn.execute("SELECT jobs_closed FROM runs ORDER BY id DESC").fetchone()[0] == 2


def test_empty_board_twice_then_new_ats(acme_on_greenhouse):
    conn = acme_on_greenhouse
    # first empty run: hold, close nothing
    out, c, router = run_once(conn, {GH_API: GH_EMPTY, CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"], c["consecutive_empty_ats_runs"], c["job_count"]) == ("OK", "greenhouse", 1, 2)
    assert "returned 0 jobs (had 2)" in c["status_reason"]
    assert CAREERS not in router.urls()  # no rediscovery yet
    # second empty run: clear the token, rediscover, find Lever
    out, c, router = run_once(conn, {GH_API: GH_EMPTY, CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"], c["consecutive_empty_ats_runs"]) == ("OK", "lever", 0)
    assert c["status_reason"] == ATS_SWITCH_NOTE
    assert len(active(conn, "greenhouse")) == 2  # not closed on the switching run
    out, c, _ = run_once(conn, {LEVER_API: LEVER_JOBS})
    assert active(conn, "greenhouse") == []


def test_empty_once_then_jobs_again_resets_counter(acme_on_greenhouse):
    conn = acme_on_greenhouse
    _, c, _ = run_once(conn, {GH_API: GH_EMPTY})
    assert c["consecutive_empty_ats_runs"] == 1
    _, c, _ = run_once(conn, {GH_API: GH_JOBS})
    assert (c["consecutive_empty_ats_runs"], c["status_reason"], c["job_count"]) == (0, "", 2)
    _, c, _ = run_once(conn, {GH_API: GH_EMPTY})
    assert c["consecutive_empty_ats_runs"] == 1 and c["ats"] == "greenhouse"


def test_empty_twice_same_board_still_linked_accepts_zero(acme_on_greenhouse):
    conn = acme_on_greenhouse
    run_once(conn, {GH_API: GH_EMPTY, CAREERS: EMBED_PAGE})
    out, c, router = run_once(conn, {GH_API: GH_EMPTY, CAREERS: EMBED_PAGE})
    assert (c["status"], c["ats"], c["ats_token"], c["job_count"]) == ("OK", "greenhouse", "acme", 0)
    assert c["status_reason"].startswith(ATS_SWITCH_NOTE) and "0 jobs accepted" in c["status_reason"]
    assert router.urls().count(GH_API) == 1  # the board isn't asked twice in one run
    assert active(conn) == []  # a genuinely empty board closes the old jobs
    assert c["consecutive_empty_ats_runs"] == 0


def test_404_board_still_linked_nothing_else(acme_on_greenhouse):
    conn = acme_on_greenhouse
    out, c, router = run_once(conn, {GH_API: GH_404, CAREERS: EMBED_PAGE})
    assert c["status"] == "NO_JOBS_FOUND"
    assert c["status_reason"] == f"{ATS_SWITCH_NOTE}; careers page still links to the old greenhouse board 'acme'"
    assert router.urls().count(GH_API) == 1
    assert len(active(conn)) == 2 and c["job_count"] == 2
    assert (c["ats"], c["ats_token"]) == (None, None)  # the dead board is not stored again


def test_dead_board_still_linked_page_jobs_close_on_the_next_run(acme_on_greenhouse):
    conn = acme_on_greenhouse
    page = (200, fixture("careers_greenhouse_embed.html").replace(
        "</body>", '<script type="application/ld+json">{"@type":"JobPosting","title":"Page Role",'
                   '"url":"https://acme.com/careers/page-role-1"}</script></body>'))
    _, c, _ = run_once(conn, {GH_API: GH_404, CAREERS: page})
    assert (c["status"], c["method"], c["ats_token"]) == ("OK", "jsonld", None)
    assert len(active(conn, "greenhouse")) == 2  # switch run: nothing closed
    # next run takes the normal path (no fast path to the dead board) and closes normally
    _, c, router = run_once(conn, {GH_API: GH_404, CAREERS: page})
    assert (c["status"], c["method"]) == ("OK", "jsonld") and not c["status_reason"].startswith(ATS_SWITCH_NOTE)
    assert active(conn) == ["https://acme.com/careers/page-role-1"]


@pytest.mark.parametrize("status", [400, 401, 422])
def test_permanent_4xx_on_stored_token_rediscovers(acme_on_greenhouse, status):
    conn = acme_on_greenhouse
    _, c, _ = run_once(conn, {GH_API: (status, "{}"), CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"], c["status_reason"]) == ("OK", "lever", ATS_SWITCH_NOTE)


def test_non_json_board_response_is_board_gone(acme_on_greenhouse):
    conn = acme_on_greenhouse
    _, c, _ = run_once(conn, {GH_API: (200, "<html>moved</html>"), CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"]) == ("OK", "lever")


def test_empty_board_twice_but_page_lists_jobs(acme_on_greenhouse):
    conn = acme_on_greenhouse
    page = (200, fixture("careers_greenhouse_embed.html").replace(
        "</body>", '<script type="application/ld+json">[{"@type":"JobPosting","title":"Role A","url":"https://acme.com/j/a"},'
                   '{"@type":"JobPosting","title":"Role B","url":"https://acme.com/j/b"}]</script></body>'))
    run_once(conn, {GH_API: GH_EMPTY, CAREERS: page})
    _, c, router = run_once(conn, {GH_API: GH_EMPTY, CAREERS: page})
    assert (c["status"], c["method"], c["ats"], c["ats_token"]) == ("OK", "jsonld", "greenhouse", None)
    assert "jobs taken from the careers page" in c["status_reason"]
    assert router.urls().count(GH_API) == 1
    assert len(active(conn, "greenhouse")) == 2  # switch run: nothing closed
    _, c, _ = run_once(conn, {GH_API: GH_EMPTY, CAREERS: page})
    assert (c["status"], c["method"], c["job_count"]) == ("OK", "jsonld", 2)
    assert active(conn) == ["https://acme.com/j/a", "https://acme.com/j/b"]


def test_fresh_company_empty_board_but_page_lists_jobs(conn):
    db.upsert_company(conn, "Acme", "acme.com")
    conn.commit()
    page = (200, fixture("careers_greenhouse_embed.html").replace(
        "</body>", '<script type="application/ld+json">{"@type":"JobPosting","title":"Role A","url":"https://acme.com/j/a"}</script></body>'))
    _, c, _ = run_once(conn, {CAREERS: page, GH_API: GH_EMPTY})
    assert (c["status"], c["method"], c["job_count"]) == ("OK", "jsonld", 1)


def test_404_board_and_discovery_fails(acme_on_greenhouse):
    conn = acme_on_greenhouse
    out, c, _ = run_once(conn, {GH_API: GH_404, "https://acme.com/": (200, "<html><body>hi</body></html>")})
    assert c["status"] == "NO_CAREERS_PAGE" and c["status_reason"].startswith(ATS_SWITCH_NOTE)
    assert (c["ats"], c["ats_token"]) == (None, None)
    assert len(active(conn)) == 2


def test_invalid_board_payload_triggers_switch(conn):
    db.upsert_company(conn, "Acme", "acme.com")
    cid = db.get_company(conn, "acme.com")["id"]
    db.update_company(conn, cid, ats="ashby", ats_token="acme")
    conn.commit()
    out, c, _ = run_once(conn, {"https://api.ashbyhq.com/posting-api/job-board/acme": (200, fixture_json("ashby_invalid_board.json")),
                                CAREERS: LEVER_PAGE, LEVER_API: LEVER_JOBS})
    assert (c["status"], c["ats"], c["status_reason"]) == ("OK", "lever", ATS_SWITCH_NOTE)


def test_transient_ats_error_keeps_token_and_jobs(acme_on_greenhouse):
    conn = acme_on_greenhouse
    out, c, router = run_once(conn, {GH_API: (500, "oops"), CAREERS: LEVER_PAGE})
    assert (c["status"], c["ats"], c["ats_token"]) == ("ERROR", "greenhouse", "acme")
    assert c["status_reason"] == "greenhouse api http 500"
    assert CAREERS not in router.urls() and len(active(conn)) == 2


def test_empty_board_for_company_without_jobs_is_plain_ok(conn):
    db.upsert_company(conn, "Acme", "acme.com")
    conn.commit()
    run_once(conn, {CAREERS: EMBED_PAGE, GH_API: GH_EMPTY})
    _, c, _ = run_once(conn, {GH_API: GH_EMPTY})
    _, c, _ = run_once(conn, {GH_API: GH_EMPTY})
    assert (c["status"], c["ats"], c["consecutive_empty_ats_runs"], c["status_reason"]) == ("OK", "greenhouse", 0, "")


def test_dead_board_still_linked_is_stable_over_runs(acme_on_greenhouse):
    """Run after run: the dead token is never stored again, so there is no fast path to it and
    no switch / allow_close=False on every other run."""
    conn = acme_on_greenhouse
    page = (200, fixture("careers_greenhouse_embed.html").replace(
        "</body>", '<script type="application/ld+json">{"@type":"JobPosting","title":"Page Role",'
                   '"url":"https://acme.com/careers/page-role-1"}</script></body>'))
    reasons = []
    for _ in range(4):
        _, c, _ = run_once(conn, {GH_API: GH_404, CAREERS: page})
        assert c["ats_token"] is None and c["status"] == "OK"
        reasons.append(c["status_reason"].startswith(ATS_SWITCH_NOTE))
    assert reasons == [True, False, False, False]
    assert active(conn) == ["https://acme.com/careers/page-role-1"]

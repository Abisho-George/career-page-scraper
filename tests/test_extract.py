import asyncio

from scraper.extract import heuristic, jsonld
from tests.helpers import Router, fixture


def test_jsonld_list_and_graph():
    jobs = jsonld.extract_jobs(fixture("jsonld_listing.html"), "https://globex.com/jobs")
    assert [j.title for j in jobs] == ["Field Sales Officer", "Remote QA Engineer"]
    a, b = jobs
    assert a.apply_url == "https://globex.com/jobs/field-sales-officer-12"
    assert a.location == "Gurgaon, India / Noida, India"
    assert a.employment_type == "Full Time, Contractor" and a.description == "Sell & grow accounts."
    assert b.apply_url == "https://globex.com/jobs/qa-engineer-13" and b.remote == "remote"
    graph = jsonld.extract_jobs(fixture("job_detail_2.html"), "https://acme.com/careers/x")
    assert graph[0].title == "Enterprise Account Executive" and graph[0].external_id == "AE-77"
    assert graph[0].location == "Bombay, MH, IN"


def test_jsonld_tolerates_broken_json():
    html = '<script type="application/ld+json">{"@type":"JobPosting","title":"Ops\nLead",}</script>'
    assert [j.title for j in jsonld.extract_jobs(html, "https://x.com/j")] == ["Ops Lead"]


def test_job_link_rules():
    links = heuristic.collect_job_links(fixture("normal_careers.html"), "https://acme.com/careers",
                                        "https://acme.com/careers", "acme.com")
    assert list(links) == [
        "https://acme.com/careers/senior-backend-engineer-1234",
        "https://acme.com/careers/enterprise-account-executive",
        "https://acme.com/careers/product-designer-ii",
    ]


def test_heuristic_end_to_end():
    r = Router({
        "https://acme.com/careers/senior-backend-engineer-1234": (200, fixture("job_detail_1.html")),
        "https://acme.com/careers/enterprise-account-executive": (200, fixture("job_detail_2.html")),
        "https://acme.com/careers/product-designer-ii": (200, fixture("job_detail_3.html")),
    })

    async def go():
        async with r.fetcher() as f:
            return await heuristic.extract_jobs(fixture("normal_careers.html"), "https://acme.com/careers", "acme.com", f)

    jobs = asyncio.run(go())
    by_title = {j.title: j for j in jobs}
    assert set(by_title) == {"Senior Backend Engineer", "Enterprise Account Executive", "Product Designer II"}
    be = by_title["Senior Backend Engineer"]
    assert be.apply_url == "https://acme.com/careers/senior-backend-engineer-1234"
    assert be.location.startswith("Bangalore, India") and "Postgres" in be.description
    assert be.source_method == "heuristic"
    assert by_title["Enterprise Account Executive"].external_id == "AE-77"  # JSON-LD on the detail page
    assert by_title["Product Designer II"].location.startswith("Remote")


def test_heuristic_follows_pagination():
    page1 = '<html><body><a href="/jobs/role-one-1">Role One</a><a href="/jobs?page=2">Next</a></body></html>'
    page2 = '<html><body><a href="/jobs/role-two-2">Role Two</a></body></html>'
    detail = "<html><body><h1>{}</h1><p>Responsibilities: stuff. Location: Pune</p></body></html>"
    r = Router({
        "https://x.com/jobs?page=2": (200, page2),
        "https://x.com/jobs/role-one-1": (200, detail.format("Role One")),
        "https://x.com/jobs/role-two-2": (200, detail.format("Role Two")),
    })

    async def go():
        async with r.fetcher() as f:
            return await heuristic.extract_jobs(page1, "https://x.com/jobs", "x.com", f)

    jobs = asyncio.run(go())
    assert sorted(j.title for j in jobs) == ["Role One", "Role Two"]
    assert all(j.location == "Pune" for j in jobs)


def test_detail_page_without_job_signals_is_dropped():
    async def go():
        return await heuristic.parse_detail(fixture("life_at_acme.html"), "https://acme.com/careers/x-y", "x", "heuristic")
    assert asyncio.run(go()) is None

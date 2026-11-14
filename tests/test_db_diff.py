from scraper import db, diff
from scraper.ats.base import Job


def make_jobs(*titles):
    return [Job(title=t, apply_url=f"https://x.com/{t}", external_id=t, source_method="ats_api") for t in titles]


def test_upsert_company_and_sheet_url(conn):
    cid = db.upsert_company(conn, "Acme", "acme.com", "")
    assert db.upsert_company(conn, "Acme Inc", "acme.com", "https://acme.com/jobs") == cid
    row = db.get_company(conn, "acme.com")
    assert row["name"] == "Acme Inc" and row["careers_url_source"] == "sheet" and row["status"] == "PENDING"
    db.upsert_company(conn, "Acme Inc", "acme.com", "")  # removed from the sheet -> forget it
    assert db.get_company(conn, "acme.com")["careers_url"] is None


def test_runs_and_resume(conn):
    for d in ("a.com", "b.com", "c.com"):
        db.upsert_company(conn, d, d)
    run_id, resumed = db.start_run(conn)
    assert not resumed
    first = db.get_companies_to_process(conn, run_id)
    assert [c["website"] for c in first] == ["a.com", "b.com", "c.com"]
    db.update_company(conn, first[0]["id"], status="OK", last_run_id=run_id, last_checked=db.now_iso())
    # "crash": run never finished -> next start resumes it and skips a.com
    run_again, resumed = db.start_run(conn)
    assert run_again == run_id and resumed
    assert [c["website"] for c in db.get_companies_to_process(conn, run_id)] == ["b.com", "c.com"]
    fresh, resumed = db.start_run(conn, fresh=True)
    assert fresh != run_id and not resumed
    # least recently checked first: a.com was checked, so it goes last
    assert [c["website"] for c in db.get_companies_to_process(conn, fresh)] == ["b.com", "c.com", "a.com"]


def test_diff_new_seen_closed(conn):
    cid = db.upsert_company(conn, "X", "x.com")
    r1 = diff.apply_jobs(conn, cid, "x.com", make_jobs("a", "b", "c"), "t1", company_ok=True, method="ats_api")
    assert (r1.new, r1.closed, r1.active) == (3, 0, 3)
    r2 = diff.apply_jobs(conn, cid, "x.com", make_jobs("a", "b", "c"), "t2", company_ok=True, method="ats_api")
    assert (r2.new, r2.closed) == (0, 0)
    r3 = diff.apply_jobs(conn, cid, "x.com", make_jobs("a", "b"), "t3", company_ok=True, method="ats_api")
    assert (r3.new, r3.closed, r3.active) == (0, 1, 2)
    first_seen = conn.execute("SELECT first_seen FROM jobs WHERE title='a'").fetchone()[0]
    assert first_seen == "t1"
    # deleting a row brings it back as new
    conn.execute("DELETE FROM jobs WHERE title = 'a'")
    r4 = diff.apply_jobs(conn, cid, "x.com", make_jobs("a", "b"), "t4", company_ok=True, method="ats_api")
    assert r4.new == 1


def test_never_close_when_not_ok(conn):
    cid = db.upsert_company(conn, "X", "x.com")
    diff.apply_jobs(conn, cid, "x.com", make_jobs("a", "b"), "t1", company_ok=True, method="ats_api")
    r = diff.apply_jobs(conn, cid, "x.com", [], "t2", company_ok=False, method="")
    assert r.closed == 0 and r.active == 2


def test_close_guard_for_scraped_pages(conn):
    cid = db.upsert_company(conn, "X", "x.com")
    titles = [f"j{i}" for i in range(12)]
    diff.apply_jobs(conn, cid, "x.com", make_jobs(*titles), "t1", company_ok=True, method="heuristic")
    r = diff.apply_jobs(conn, cid, "x.com", make_jobs("j1", "j2"), "t2", company_ok=True, method="heuristic")
    assert r.close_skipped and r.closed == 0 and r.active == 12
    r = diff.apply_jobs(conn, cid, "x.com", make_jobs("j1", "j2"), "t3", company_ok=True, method="ats_api")
    assert r.closed == 10  # an ATS saying fewer jobs is trusted


def test_empty_field_does_not_overwrite(conn):
    cid = db.upsert_company(conn, "X", "x.com")
    jid = diff.make_job_id("x.com", "a")
    db.upsert_job(conn, cid, jid, {"title": "A", "description": "long text", "apply_url": "u"}, "t1")
    db.upsert_job(conn, cid, jid, {"title": "A2", "description": "", "apply_url": "u"}, "t2")
    row = conn.execute("SELECT title, description FROM jobs WHERE job_id = ?", (jid,)).fetchone()
    assert tuple(row) == ("A2", "long text")
    known = diff.known_with_description(conn, cid, "x.com")
    assert known("a") and not known("b")


def test_finish_run_counts(conn):
    run_id, _ = db.start_run(conn)
    for d, status, method, ats in [("a.com", "OK", "ats_api", "lever"), ("b.com", "OK", "heuristic", "custom"),
                                   ("c.com", "BLOCKED", None, None), ("d.com", "NO_JOBS_FOUND", None, "darwinbox"),
                                   ("e.com", "NO_JOBS_FOUND", None, "darwinbox"), ("f.com", "ERROR", None, None)]:
        cid = db.upsert_company(conn, d, d)
        db.update_company(conn, cid, status=status, method=method, ats=ats, last_run_id=run_id)
    db.add_run_counts(conn, run_id, jobs_new=5, jobs_closed=1)
    run = db.finish_run(conn, run_id)
    assert (run["companies_total"], run["ok"], run["blocked"], run["failed"], run["no_jobs"]) == (6, 2, 1, 1, 2)
    assert (run["ok_ats_api"], run["ok_heuristic"], run["jobs_new"]) == (1, 1, 5)
    assert run["top_no_jobs_ats"] == "darwinbox (2)"

"""CLI: python main.py run | run --limit 20 | company <domain> | sheets"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import config
from scraper import db, sheets
from scraper.browser import Browser
from scraper.fetch import Fetcher
from scraper.pipeline import Context, process_company

log = logging.getLogger("main")

STATUS_ORDER = {"ERROR": 0, "BLOCKED": 1, "ROBOTS_DISALLOWED": 2, "NO_CAREERS_PAGE": 3, "NO_JOBS_FOUND": 4,
                "PENDING": 5, "OK": 6}


def setup_logging(verbose: bool = False) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    logfile = config.LOG_DIR / f"run-{datetime.now():%Y%m%d}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "trafilatura", "urllib3", "asyncio", "charset_normalizer", "htmldate",
                  "courlan", "justext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ------------------------------------------------------------------ sheet output

def build_tabs(conn: sqlite3.Connection) -> dict[str, list[list]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.JOBS_TAB_CLOSED_DAYS)).replace(microsecond=0).isoformat()
    jobs = conn.execute(
        """SELECT c.name AS company, c.website, j.title, j.location, j.remote, j.department, j.employment_type,
                  j.description, j.apply_url, j.posted_at, j.first_seen, j.last_seen, j.is_active, j.source_method
           FROM jobs j JOIN companies c ON c.id = j.company_id
           WHERE j.is_active = 1 OR j.closed_at >= ?
           ORDER BY j.is_active DESC, j.first_seen DESC, c.name, j.title""",
        (cutoff,),
    ).fetchall()
    job_rows = [["company", "website", "title", "location", "remote", "department", "employment_type",
                 "description", "apply_url", "posted_at", "first_seen", "last_seen", "status", "source_method"]]
    for j in jobs:
        job_rows.append([
            j["company"], j["website"], j["title"], j["location"], j["remote"], j["department"],
            j["employment_type"], (j["description"] or "")[: config.SHEET_DESCRIPTION_CHARS], j["apply_url"],
            j["posted_at"], j["first_seen"], j["last_seen"], "active" if j["is_active"] else "closed",
            j["source_method"],
        ])

    companies = conn.execute("SELECT * FROM companies").fetchall()
    companies = sorted(companies, key=lambda c: (STATUS_ORDER.get(c["status"], 9), c["name"] or ""))
    status_rows = [["company_name", "website", "status", "reason", "job_count", "careers_url", "ats",
                    "method", "last_checked"]]
    for c in companies:
        status_rows.append([c["name"], c["website"], c["status"], c["status_reason"] or "", c["job_count"] or 0,
                            c["careers_url"] or "", c["ats"] or "", c["method"] or "", c["last_checked"] or ""])

    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
    run_cols = ["id", "started_at", "finished_at", "companies_total", "ok", "blocked", "robots_disallowed", "failed",
                "no_jobs", "no_careers", "jobs_new", "jobs_closed", "ok_ats_api", "ok_jsonld", "ok_heuristic", "ok_browser",
                "top_no_jobs_ats"]
    run_rows = [run_cols] + [[r[c] if r[c] is not None else "" for c in run_cols] for r in runs]
    return {"Jobs": job_rows, "Company Status": status_rows, "Runs": run_rows}


def write_sheets(conn: sqlite3.Connection) -> bool:
    try:
        spreadsheet = sheets.open_sheet()
        for name, rows in build_tabs(conn).items():
            sheets.write_tab(name, rows, spreadsheet)
        return True
    except Exception as e:  # noqa: BLE001 - data is safe in SQLite; the next run rewrites the tabs
        log.error("writing sheets failed: %s: %s", type(e).__name__, e)
        return False


# ------------------------------------------------------------------ commands

def load_companies(args) -> list[dict] | None:
    if args.csv:
        return sheets.read_companies_csv(args.csv)
    if not config.SHEET_ID:
        log.error("SHEET_ID is not set. Set it in .env, or use --csv companies.csv")
        return None
    return sheets.read_companies()


async def run_companies(conn: sqlite3.Connection, run_id: int, companies: list[sqlite3.Row],
                        use_browser: bool) -> None:
    sem = asyncio.Semaphore(config.MAX_PARALLEL_COMPANIES)
    browser = Browser() if use_browser else None
    async with Fetcher() as fetcher:
        ctx = Context(conn=conn, fetcher=fetcher, browser=browser, run_id=run_id)

        async def worker(company):
            async with sem:
                await process_company(ctx, company)

        try:
            await asyncio.gather(*(worker(c) for c in companies))
        finally:
            if browser is not None:
                await browser.close()


def cmd_run(args) -> int:
    conn = db.connect()
    websites = None
    try:
        companies = load_companies(args)
    except Exception as e:  # noqa: BLE001
        log.error("reading companies failed (%s: %s); continuing with companies already in the database",
                  type(e).__name__, e)
        companies = []
    if companies is None:
        return 2
    if companies:
        for c in companies:
            db.upsert_company(conn, c["company_name"], c["website"], c["careers_url"])
        conn.commit()
        websites = {c["website"] for c in companies}
        log.info("%d companies in the input list", len(companies))

    run_id, resumed = db.start_run(conn, fresh=args.fresh)
    limit = args.limit or config.MAX_COMPANIES_PER_RUN
    todo = db.get_companies_to_process(conn, run_id, limit, websites)
    log.info("run %d %s: %d companies to process", run_id, "resumed" if resumed else "started", len(todo))
    if not todo and not websites and not conn.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
        log.error("no companies to process")
        return 1

    asyncio.run(run_companies(conn, run_id, todo, use_browser=config.USE_BROWSER and not args.no_browser))
    run = db.finish_run(conn, run_id)
    sheets_ok = True if args.no_sheets else write_sheets(conn)
    print(
        f"\nRun {run['id']}: {run['companies_total']} companies | OK {run['ok']} | BLOCKED {run['blocked']} | "
        f"ROBOTS_DISALLOWED {run['robots_disallowed']} | "
        f"NO_JOBS_FOUND {run['no_jobs']} | NO_CAREERS_PAGE {run['no_careers']} | ERROR {run['failed']} | "
        f"jobs new {run['jobs_new']}, closed {run['jobs_closed']}"
        f"{'' if sheets_ok else ' | SHEETS NOT UPDATED (see log)'}"
    )
    return 0 if sheets_ok else 3  # non-zero so a scheduled run shows as failed and GitHub emails you


def cmd_company(args) -> int:
    conn = db.connect()
    domain = sheets.normalize_domain(args.domain)
    if not domain:
        print("invalid domain")
        return 2
    if db.get_company(conn, domain) is None or args.careers_url:
        db.upsert_company(conn, domain, domain, args.careers_url or "")
        conn.commit()
    company = db.get_company(conn, domain)
    open_run = db.get_open_run(conn)
    # join an unfinished run if there is one; otherwise record a one-company run
    run_id = open_run["id"] if open_run else db.start_run(conn, fresh=True)[0]

    async def go():
        browser = None if args.no_browser or not config.USE_BROWSER else Browser()
        async with Fetcher() as fetcher:
            try:
                return await process_company(Context(conn, fetcher, browser, run_id), company)
            finally:
                if browser:
                    await browser.close()

    outcome = asyncio.run(go())
    if not open_run:
        db.finish_run(conn, run_id)
    row = db.get_company(conn, domain)
    print(f"\n{domain}: {row['status']} ({row['status_reason'] or '-'})")
    print(f"careers_url: {row['careers_url']}  ats: {row['ats']}  token: {row['ats_token']}  method: {row['method']}")
    jobs = conn.execute(
        "SELECT title, location, remote, apply_url, posted_at FROM jobs WHERE company_id = ? AND is_active = 1 "
        "ORDER BY title", (row["id"],)).fetchall()
    print(f"{len(jobs)} active jobs ({len(outcome.jobs)} extracted this time)")
    for j in jobs[: args.show]:
        print(f"  - {j['title'][:70]:70} | {(j['location'] or '')[:30]:30} | {j['remote'] or '':7} | {j['apply_url']}")
    if len(jobs) > args.show:
        print(f"  ... {len(jobs) - args.show} more")
    return 0


def cmd_sheets(_args) -> int:
    conn = db.connect()
    return 0 if write_sheets(conn) else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="main.py", description="Career page job scraper")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process companies (resumes an unfinished run)")
    r.add_argument("--limit", type=int, default=0, help="process at most N companies")
    r.add_argument("--fresh", action="store_true", help="start a new run even if the last one did not finish")
    r.add_argument("--csv", help="read companies from a CSV (company_name,website,careers_url) instead of the Sheet")
    r.add_argument("--no-sheets", action="store_true", help="don't write the Sheet tabs at the end")
    r.add_argument("--no-browser", action="store_true", help="skip the Playwright fallback")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("company", help="debug one company with a verbose trace")
    c.add_argument("domain")
    c.add_argument("--careers-url", default="", help="use this careers URL (saved as if it came from the sheet)")
    c.add_argument("--no-browser", action="store_true")
    c.add_argument("--show", type=int, default=25, help="how many jobs to print")
    c.set_defaults(func=cmd_company, verbose=True)

    s = sub.add_parser("sheets", help="rewrite the Jobs / Company Status / Runs tabs from SQLite")
    s.set_defaults(func=cmd_sheets, verbose=False)

    args = p.parse_args(argv)
    setup_logging(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

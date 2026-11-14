"""Regression tests from real recordings: every folder in tests/fixtures/replay/ (copied from
tests/fixtures/live/ after `python main.py company <domain> --save-fixtures`) is replayed offline
and must give the same status, ATS, token, method and job count as its summary.json."""
import asyncio
from pathlib import Path

import pytest

from scraper import db
from scraper.fetch import Response
from scraper.pipeline import Context, process_company
from tests.helpers import load_recording

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
CASES = sorted(p for p in REPLAY_DIR.glob("*") if (p / "index.json").exists() and (p / "summary.json").exists()) \
    if REPLAY_DIR.exists() else []


class ReplayBrowser:
    def __init__(self, renders):
        self.renders = renders

    async def render(self, url):
        if url not in self.renders:
            raise RuntimeError(f"no saved render for {url}")
        r = self.renders[url]
        return Response(url=r["final_url"], status=r["status"], headers=r["headers"], text=r["text"])


@pytest.mark.skipif(not CASES, reason="no recordings in tests/fixtures/replay/ yet (see LIVE_TEST.md)")
@pytest.mark.parametrize("folder", CASES, ids=[p.name for p in CASES])
def test_replay(folder, conn):
    router, renders, summary = load_recording(folder)
    db.upsert_company(conn, summary.get("company_name") or folder.name, summary["domain"],
                      summary["careers_url"] if summary.get("careers_url_from_sheet") else "")
    conn.commit()

    async def go():
        run_id, _ = db.start_run(conn)
        async with router.fetcher() as f:
            browser = ReplayBrowser(renders) if renders else None
            await process_company(Context(conn, f, browser, run_id), db.get_company(conn, summary["domain"]))
    asyncio.run(go())
    c = db.get_company(conn, summary["domain"])
    got = {k: c[k] for k in ("status", "ats", "ats_token", "method", "job_count")}
    want = {k: summary[k] for k in ("status", "ats", "ats_token", "method", "job_count")}
    assert got == want

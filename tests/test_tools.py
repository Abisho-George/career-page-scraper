"""--save-fixtures recording + replay, and check-sheets error messages."""
import asyncio
import json

import httpx
import pytest

import config
import main
from scraper import db, sheets
from scraper.pipeline import Context, process_company
from tests.helpers import Router, fixture, fixture_json, load_recording

GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def dead(req):
    return httpx.ConnectError("dns failure")


# acme.com: apex doesn't resolve -> www; /careers redirects to /en/careers (Greenhouse embed)
LIVE = {
    **{f"https://acme.com{p}": dead for p in ["/robots.txt"] + config.CAREERS_PATHS},
    "https://www.acme.com/careers": (301, "", {"location": "https://www.acme.com/en/careers"}),
    "https://www.acme.com/en/careers": (200, fixture("careers_greenhouse_embed.html"), {"content-encoding": "identity"}),
    GH_API: (200, fixture_json("greenhouse_jobs.json")),
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "USE_BROWSER", False)
    monkeypatch.setattr(main, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(config, "MAX_RETRIES", 0)  # dead hosts fail at once instead of retrying
    return tmp_path


def record_acme(monkeypatch, routes=LIVE):
    routers = []

    def fake_fetcher(*a, **k):
        r = Router(routes)
        routers.append(r)
        return r.fetcher(recorder=k.get("recorder"))
    monkeypatch.setattr(main, "Fetcher", fake_fetcher)
    assert main.main(["company", "acme.com", "--save-fixtures"]) == 0
    return routers[0]


def test_save_fixtures_writes_readable_files_and_summary(isolated, monkeypatch, capsys):
    router = record_acme(monkeypatch)
    folder = isolated / "live" / "acme.com"
    files = sorted(p.name for p in folder.iterdir())
    assert "index.json" in files and "summary.json" in files
    names = [f for f in files if f[:3].isdigit()]
    assert len(names) == len(router.calls)  # one file per request, redirect hops included
    assert any(n.endswith("_GET_boards-api.greenhouse.io_v1_boards_acme_jobs_content-true.json") for n in names)
    assert any("_GET-ERROR_acme.com_careers" in n for n in names)
    assert any(n.endswith("_GET_www.acme.com_en_careers.html") for n in names)
    saved_json = json.loads(next((folder / n) for n in names if "boards-api" in n and "robots" not in n).read_text())
    assert saved_json["jobs"][0]["title"] == "Senior Backend Engineer"
    summary = json.loads((folder / "summary.json").read_text())
    assert {k: summary[k] for k in ("domain", "status", "ats", "ats_token", "method", "job_count")} == {
        "domain": "acme.com", "status": "OK", "ats": "greenhouse", "ats_token": "acme", "method": "ats_api", "job_count": 2}
    assert summary["files"] == len(names) and len(summary["sample_jobs"]) == 2
    index = json.loads((folder / "index.json").read_text())["entries"]
    hop = next(e for e in index if e["url"] == "https://www.acme.com/careers")
    assert hop["status"] == 301 and hop["headers"]["location"] == "https://www.acme.com/en/careers"
    assert "saved" in capsys.readouterr().out
    # the real database was not created
    assert not config.DB_PATH.exists()


def test_save_fixtures_reads_existing_db_without_changing_it(isolated, monkeypatch):
    import sqlite3
    old = sqlite3.connect(config.DB_PATH)
    old.executescript("""CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, website TEXT UNIQUE NOT NULL,
        careers_url TEXT, careers_url_source TEXT, ats TEXT, ats_token TEXT, status TEXT, status_reason TEXT,
        method TEXT, job_count INTEGER, last_checked TEXT, last_run_id INTEGER);
        INSERT INTO companies (name, website, careers_url, careers_url_source) VALUES
        ('Acme Corp', 'acme.com', 'https://www.acme.com/en/careers', 'sheet');""")
    old.commit()
    old.close()
    before = config.DB_PATH.read_bytes()
    router = record_acme(monkeypatch)
    assert config.DB_PATH.read_bytes() == before  # not migrated, not written
    assert router.urls()[1] == "https://www.acme.com/en/careers"  # used the sheet careers_url
    summary = json.loads((isolated / "live" / "acme.com" / "summary.json").read_text())
    assert summary["company_name"] == "Acme Corp" and summary["careers_url_from_sheet"]


def test_bodies_saved_exactly_as_received(isolated, monkeypatch):
    record_acme(monkeypatch)
    folder = isolated / "live" / "acme.com"
    saved = next(p for p in folder.iterdir() if "boards-api" in p.name and "robots" not in p.name)
    assert saved.read_text() == json.dumps(fixture_json("greenhouse_jobs.json"))


def test_rerun_replaces_previous_recording(isolated, monkeypatch):
    record_acme(monkeypatch)
    folder = isolated / "live" / "acme.com"
    (folder / "stale.txt").write_text("x")
    record_acme(monkeypatch)
    assert not (folder / "stale.txt").exists()


def test_recording_replays_to_the_same_result(isolated, monkeypatch, conn):
    record_acme(monkeypatch)
    router, renders, summary = load_recording(isolated / "live" / "acme.com")
    db.upsert_company(conn, "acme.com", "acme.com")
    conn.commit()

    async def go():
        run_id, _ = db.start_run(conn)
        async with router.fetcher() as f:
            await process_company(Context(conn, f, None, run_id), db.get_company(conn, "acme.com"))
    asyncio.run(go())
    c = db.get_company(conn, "acme.com")
    assert (c["status"], c["ats"], c["ats_token"], c["method"], c["job_count"]) == (
        summary["status"], summary["ats"], summary["ats_token"], summary["method"], summary["job_count"])


def test_replay_matches_post_bodies(tmp_path):
    from scraper.recorder import FixtureRecorder
    rec = FixtureRecorder(tmp_path / "wd")
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    rec.record("POST", url, 200, {"content-type": "application/json"}, '{"page": 1}', request_body='{"offset": 0}')
    rec.record("POST", url, 200, {"content-type": "application/json"}, '{"page": 2}', request_body='{"offset": 20}')
    router, _, _ = load_recording(tmp_path / "wd")

    async def go():
        async with router.fetcher(respect_robots=False) as f:
            r2 = await f.request("POST", url, content='{"offset": 20}')
            r1 = await f.request("POST", url, content='{"offset": 0}')
            return r1.json(), r2.json()
    assert asyncio.run(go()) == ({"page": 1}, {"page": 2})


# ---------------------------------------------------------------- check-sheets

class FakeResponse:
    def __init__(self, code, status, message):
        self.status_code, self._body = code, {"error": {"code": code, "status": status, "message": message}}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def api_error(code, status, message):
    import gspread
    return gspread.exceptions.APIError(FakeResponse(code, status, message))


class FakeWS:
    def __init__(self, title, values=None, fail_write=None):
        self.title, self.values, self.row_count, self.col_count = title, values or [], 100, 3
        self.fail_write = fail_write
        self.written = None

    def get_values(self, rng):
        return self.values

    def update(self, values, range_name, value_input_option):
        self.written = values


class FakeSpreadsheet:
    def __init__(self, tabs, fail_write=None):
        self.title, self.tabs, self.fail_write, self.deleted = "Job Scraper", tabs, fail_write, []

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, name):
        import gspread
        if name not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(name)
        return self.tabs[name]

    def add_worksheet(self, title, rows, cols):
        if self.fail_write:
            raise self.fail_write
        ws = FakeWS(title)
        ws.get_values = lambda rng: ws.written
        self.tabs[title] = ws
        return ws

    def del_worksheet(self, ws):
        self.deleted.append(ws.title)
        del self.tabs[ws.title]


COMPANIES = [["company_name", "website", "careers_url"], ["Acme", "acme.com", ""], ["Globex", "globex.com", ""]]


class FakeClient:
    def __init__(self, open_error=None, spreadsheet=None, drive_error=None):
        self.open_error, self.spreadsheet, self.drive_error = open_error, spreadsheet, drive_error

    def open_by_key(self, key):
        if self.open_error:
            raise self.open_error
        return self.spreadsheet

    def get_file_drive_metadata(self, key):
        if self.drive_error:
            raise self.drive_error
        return {}


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "service_account.json"
    path.write_text(json.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
                                "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
                                "project_id": "job-scraper"}))
    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(path))
    monkeypatch.setattr(config, "SHEET_ID", "SHEET123")
    monkeypatch.setattr(config, "CONTACT_EMAIL", "me@example.org")
    return path


def check(client):
    lines = []
    ok = sheets.check_sheets(say=lines.append, client_factory=lambda: client)
    return ok, "\n".join(lines)


def test_check_sheets_happy_path(key_file):
    ss = FakeSpreadsheet({"Companies": FakeWS("Companies", COMPANIES)})
    ok, out = check(FakeClient(spreadsheet=ss))
    assert ok, out
    assert "opened 'Job Scraper'" in out and "tab 'Companies'" in out and "['Acme', 'acme.com', '']" in out
    assert len(ss.deleted) == 1 and ss.deleted[0].startswith("_check_sheets_") and list(ss.tabs) == ["Companies"]
    assert "Sheets check passed" in out


def test_missing_sheet_id(key_file, monkeypatch):
    monkeypatch.setattr(config, "SHEET_ID", "")
    ok, out = check(FakeClient())
    assert not ok and "SHEET_ID is empty" in out


def test_missing_key_file(key_file, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CREDENTIALS_FILE", str(tmp_path / "nope.json"))
    ok, out = check(FakeClient())
    assert not ok and "Service account key not found" in out


def test_wrong_key_type(key_file):
    key_file.write_text(json.dumps({"type": "authorized_user", "client_id": "x"}))
    ok, out = check(FakeClient())
    assert not ok and "not a service-account key" in out


def test_contact_email_warning(key_file, monkeypatch):
    monkeypatch.setattr(config, "CONTACT_EMAIL", "unset@example.com")
    ss = FakeSpreadsheet({"Companies": FakeWS("Companies", COMPANIES)})
    ok, out = check(FakeClient(spreadsheet=ss))
    assert ok and "WARN  CONTACT_EMAIL is not set" in out


def test_sheet_not_shared(key_file):
    err = PermissionError()
    err.__cause__ = api_error(403, "PERMISSION_DENIED", "The caller does not have permission")
    ok, out = check(FakeClient(open_error=err))
    assert not ok and "not shared with the service account" in out
    assert "bot@proj.iam.gserviceaccount.com as Editor" in out


def test_sheets_api_not_enabled(key_file):
    err = PermissionError()
    err.__cause__ = api_error(403, "PERMISSION_DENIED",
                              "Google Sheets API has not been used in project 123456 before or it is disabled. "
                              "Enable it by visiting https://console.developers.google.com/apis/api/sheets.googleapis.com")
    ok, out = check(FakeClient(open_error=err))
    assert not ok and "Google Sheets API is not enabled for Google Cloud project 'job-scraper'" in out
    assert "apis/library/sheets.googleapis.com?project=job-scraper" in out


def test_wrong_sheet_id(key_file):
    import gspread
    ok, out = check(FakeClient(open_error=gspread.exceptions.SpreadsheetNotFound("x")))
    assert not ok and "No spreadsheet found" in out and "Check SHEET_ID" in out


def test_missing_companies_tab(key_file):
    ok, out = check(FakeClient(spreadsheet=FakeSpreadsheet({"Sheet1": FakeWS("Sheet1")})))
    assert not ok and "No tab named 'Companies'" in out


def test_bad_headers(key_file):
    ss = FakeSpreadsheet({"Companies": FakeWS("Companies", [["Name", "URL"], ["Acme", "acme.com"]])})
    ok, out = check(FakeClient(spreadsheet=ss))
    assert not ok and "missing: company_name, website" in out


def test_viewer_only_access(key_file):
    ss = FakeSpreadsheet({"Companies": FakeWS("Companies", COMPANIES)},
                         fail_write=api_error(403, "PERMISSION_DENIED", "The caller does not have permission"))
    ok, out = check(FakeClient(spreadsheet=ss))
    assert not ok and "can read the sheet but not" in out and "change bot@proj.iam.gserviceaccount.com to Editor" in out


def test_drive_api_disabled_is_only_a_warning(key_file):
    ss = FakeSpreadsheet({"Companies": FakeWS("Companies", COMPANIES)})
    drive = api_error(403, "PERMISSION_DENIED", "Google Drive API has not been used in project 123456 before or it is disabled.")
    ok, out = check(FakeClient(spreadsheet=ss, drive_error=drive))
    assert ok and "WARN  Google Drive API is not enabled" in out


def test_check_sheets_command_exit_code(key_file, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "SHEET_ID", "")
    assert main.main(["check-sheets"]) == 1


def _rec(tmp_path):
    from scraper.recorder import FixtureRecorder
    return FixtureRecorder(tmp_path / "rec")


def test_replay_retry_order_per_request(tmp_path):
    rec = _rec(tmp_path)
    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    rec.record("POST", url, 0, {}, "", request_body='{"offset": 0}', kind="error", error="ReadTimeout")
    rec.record("POST", url, 200, {"content-type": "application/json"}, '{"page": 1}', request_body='{"offset": 0}')
    rec.record("POST", url, 200, {"content-type": "application/json"}, '{"page": 2}', request_body='{"offset": 20}')
    router, _, _ = load_recording(tmp_path / "rec")

    async def go():
        async with router.fetcher(respect_robots=False) as f:  # retries the ReadTimeout once
            r1 = await f.request("POST", url, content='{"offset": 0}')
            r2 = await f.request("POST", url, content='{"offset": 20}')
            return r1.json(), r2.json()
    assert asyncio.run(go()) == ({"page": 1}, {"page": 2})


def test_recorded_error_url_is_normalised(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 0)
    rec = _rec(tmp_path)

    async def live():
        async with Router({"https://careers.acme.com": dead}).fetcher(recorder=rec, respect_robots=False) as f:
            with pytest.raises(httpx.ConnectError):
                await f.get("https://Careers.Acme.com")
    asyncio.run(live())
    router, _, _ = load_recording(tmp_path / "rec")

    async def replay():
        async with router.fetcher(respect_robots=False) as f:
            with pytest.raises(httpx.ConnectError):
                await f.get("https://Careers.Acme.com")
    asyncio.run(replay())


def test_robots_without_content_type_replays_as_robots(tmp_path):
    rec = _rec(tmp_path)
    rec.record("GET", "https://acme.com/robots.txt", 200, {}, "User-agent: *\nDisallow: /careers\n")
    router, _, _ = load_recording(tmp_path / "rec")

    async def go():
        async with router.fetcher() as f:
            return await f.allowed("https://acme.com/careers")
    assert asyncio.run(go()) is False


def test_rendered_page_replays_status_headers_and_final_url(tmp_path):
    from tests.test_replay import ReplayBrowser
    rec = _rec(tmp_path)
    rec.record("GET", "https://acme.com/careers", 403, {"cf-mitigated": "challenge"}, "<html>no</html>",
               kind="rendered", final_url="https://acme.com/en/careers")
    _, renders, _ = load_recording(tmp_path / "rec")
    r = asyncio.run(ReplayBrowser(renders).render("https://acme.com/careers"))
    assert (r.status, r.headers, r.url) == (403, {"cf-mitigated": "challenge"}, "https://acme.com/en/careers")

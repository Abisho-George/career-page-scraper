"""Google Sheets in/out via gspread + a service account, plus a CSV reader for local runs."""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import config

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CHUNK_ROWS = 5000
MAX_CELL = 49_000  # Sheets hard limit is 50,000 characters per cell


def normalize_domain(website: str) -> str:
    """'https://www.Acme.com/about?x' -> 'acme.com'"""
    w = (website or "").strip().lower()
    if not w:
        return ""
    if "://" not in w:
        w = "http://" + w
    host = urlsplit(w).hostname or ""
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _rows_to_companies(records: list[dict]) -> list[dict]:
    out, seen = [], set()
    for rec in records:
        rec = {re.sub(r"\s+", "_", str(k).strip().lower()): (str(v).strip() if v is not None else "")
               for k, v in rec.items()}
        domain = normalize_domain(rec.get("website", ""))
        if not domain:
            domain = normalize_domain(rec.get("careers_url", ""))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        careers = rec.get("careers_url", "")
        if careers and "://" not in careers:
            careers = "https://" + careers
        out.append({
            "company_name": rec.get("company_name") or domain,
            "website": domain,
            "careers_url": careers,
        })
    return out


def read_companies_csv(path: str | Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _rows_to_companies(list(csv.DictReader(f)))


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(config.CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet(sheet_id: str | None = None):
    sheet_id = sheet_id or config.SHEET_ID
    if not sheet_id:
        raise RuntimeError("SHEET_ID is not set (see .env.example)")
    return _client().open_by_key(sheet_id)


def read_companies(spreadsheet=None) -> list[dict]:
    """Read tab 'Companies' (company_name | website | careers_url), website -> bare domain."""
    spreadsheet = spreadsheet or open_sheet()
    ws = spreadsheet.worksheet("Companies")
    return _rows_to_companies(ws.get_all_records())


def _cell(v) -> str | int | float:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return s[:MAX_CELL]


def write_tab(name: str, rows: list[list], spreadsheet=None) -> None:
    """Clear and rewrite a tab (header row first). Creates the tab if missing.
    Values are written RAW so text like '=foo' is never run as a formula."""
    spreadsheet = spreadsheet or open_sheet()
    try:
        import gspread
        not_found = gspread.exceptions.WorksheetNotFound
    except ImportError:  # pragma: no cover
        not_found = Exception
    n_rows = max(len(rows), 1)
    n_cols = max((len(r) for r in rows), default=1)
    try:
        ws = spreadsheet.worksheet(name)
    except not_found:
        ws = spreadsheet.add_worksheet(title=name, rows=n_rows + 1, cols=n_cols)
    values = [[_cell(v) for v in r] for r in rows]
    ws.clear()
    ws.resize(rows=n_rows + 1, cols=n_cols)
    for start in range(0, len(values), CHUNK_ROWS):
        chunk = values[start:start + CHUNK_ROWS]
        ws.update(values=chunk, range_name=f"A{start + 1}", value_input_option="RAW")
    log.info("sheet tab %r written: %d rows", name, len(values) - 1)


# ------------------------------------------------------------------ check-sheets

REQUIRED_HEADERS = ("company_name", "website")
PLACEHOLDER_EMAILS = ("", "unset@example.com", "you@example.com")


class CheckFailed(Exception):
    """A setup problem, with a message that says how to fix it."""


def _api_error_info(exc: BaseException) -> tuple[int, str, str]:
    """(code, status, message) from a gspread APIError, also when gspread wrapped it in a
    PermissionError (open_by_key does that for 403)."""
    import gspread

    err = exc if isinstance(exc, gspread.exceptions.APIError) else getattr(exc, "__cause__", None)
    if isinstance(err, gspread.exceptions.APIError):
        info = err.error or {}
        details = " ".join(str(d.get("reason", "")) for d in info.get("details", []) if isinstance(d, dict))
        return int(info.get("code") or err.code or 0), f"{info.get('status', '')} {details}".strip(), str(info.get("message", ""))
    return 0, "", str(exc)


def _disabled_api(status: str, message: str) -> str | None:
    """Name of the Google API that is not enabled, if that's what the error says."""
    text = f"{status} {message}"
    if not re.search(r"SERVICE_DISABLED|accessNotConfigured|has not been used in project|is disabled", text, re.I):
        return None
    m = re.search(r"(Google [A-Za-z ]+? API)", message)
    return m.group(1) if m else "Google Sheets API"


def _explain(exc: BaseException, what: str, client_email: str, project: str) -> CheckFailed:
    import gspread

    code, status, message = _api_error_info(exc)
    api = _disabled_api(status, message)
    if api:
        slug = "drive.googleapis.com" if "Drive" in api else "sheets.googleapis.com"
        return CheckFailed(
            f"{api} is not enabled for Google Cloud project '{project}'.\n"
            f"  Enable it: https://console.cloud.google.com/apis/library/{slug}?project={project}\n"
            f"  (enable both the Google Sheets API and the Google Drive API), wait a minute, run again.")
    if isinstance(exc, gspread.exceptions.SpreadsheetNotFound) or code == 404:
        return CheckFailed(
            f"No spreadsheet found while trying to {what}.\n"
            f"  Check SHEET_ID in .env (the part of the sheet URL between /d/ and /edit), and share the\n"
            f"  sheet with {client_email} (Share > add that address > Editor).")
    if (isinstance(exc, PermissionError) or code == 403) and "write" in what:
        return CheckFailed(
            f"Permission denied while trying to {what}: the service account can read the sheet but not\n"
            f"  edit it. Open the sheet > Share > change {client_email} to Editor.")
    if isinstance(exc, PermissionError) or code == 403:
        return CheckFailed(
            f"Permission denied while trying to {what}: the sheet is not shared with the service account.\n"
            f"  Open the sheet > Share > add {client_email} as Editor (Viewer is not enough to write).")
    if code == 429:
        return CheckFailed(f"Google API quota exceeded while trying to {what}; wait a minute and run again.")
    try:
        from google.auth.exceptions import RefreshError, TransportError
    except ImportError:  # pragma: no cover
        RefreshError = TransportError = ()  # type: ignore[assignment]
    if isinstance(exc, RefreshError):
        return CheckFailed(
            f"Google rejected the service account key ({message[:200]}).\n"
            f"  The key may have been deleted or disabled in Cloud Console (create a new JSON key), or this\n"
            f"  computer's clock is wrong.")
    if isinstance(exc, TransportError) or "Connection" in type(exc).__name__:
        return CheckFailed(f"Could not reach Google while trying to {what} ({type(exc).__name__}). Check the network.")
    return CheckFailed(f"Failed to {what}: {type(exc).__name__}: {message[:300]}")


def _load_key(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise CheckFailed(
            f"Service account key not found at '{path}'.\n"
            f"  Download a JSON key for the service account (Cloud Console > IAM & Admin > Service accounts >\n"
            f"  Keys > Add key > JSON), save it as service_account.json in the project folder, or set\n"
            f"  GOOGLE_APPLICATION_CREDENTIALS in .env to its path.")
    try:
        key = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise CheckFailed(f"'{path}' is not valid JSON ({e}). Download the key again.") from e
    if key.get("type") != "service_account" or not key.get("client_email") or not key.get("private_key"):
        raise CheckFailed(f"'{path}' is not a service-account key (expected \"type\": \"service_account\" "
                          f"with client_email and private_key). Download a JSON key for a service account.")
    return key


def check_sheets(say=print, client_factory=None) -> bool:
    """Connect with the service account, print the sheet title and tabs, read the first Companies
    rows, then write and delete a test tab. Returns True when everything works."""
    import gspread

    def ok(msg):
        say(f"  OK    {msg}")

    def warn(msg):
        say(f"  WARN  {msg}")

    try:
        say("1. Settings (.env)")
        if not config.SHEET_ID:
            raise CheckFailed("SHEET_ID is empty. Put the sheet ID (the part of its URL between /d/ and /edit)\n"
                              "  in .env as SHEET_ID=...  (copy .env.example to .env first if you have no .env)")
        ok(f"SHEET_ID = {config.SHEET_ID}")
        if config.CONTACT_EMAIL in PLACEHOLDER_EMAILS:
            warn("CONTACT_EMAIL is not set in .env; it goes in the scraper's User-Agent so sites can reach you")
        else:
            ok(f"CONTACT_EMAIL = {config.CONTACT_EMAIL}")
        key = _load_key(config.CREDENTIALS_FILE)
        email, project = key["client_email"], key.get("project_id", "?")
        ok(f"service account {email} (project {project})")

        say("2. Connect to the sheet")
        try:
            client = (client_factory or _client)()
            spreadsheet = client.open_by_key(config.SHEET_ID)
            title = spreadsheet.title
            tabs = spreadsheet.worksheets()
        except CheckFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise _explain(e, "open the sheet", email, project) from e
        ok(f"opened '{title}'")
        for ws in tabs:
            say(f"        tab '{ws.title}' ({ws.row_count} rows x {ws.col_count} cols)")

        say("3. Read the Companies tab")
        try:
            ws = spreadsheet.worksheet("Companies")
        except gspread.exceptions.WorksheetNotFound:
            raise CheckFailed("No tab named 'Companies'. Add one with the header row:\n"
                              "  company_name | website | careers_url") from None
        try:
            values = ws.get_values("A1:Z6")
        except Exception as e:  # noqa: BLE001
            raise _explain(e, "read the Companies tab", email, project) from e
        header = [re.sub(r"\s+", "_", str(h).strip().lower()) for h in (values[0] if values else [])]
        missing = [h for h in REQUIRED_HEADERS if h not in header]
        if missing:
            raise CheckFailed(f"The Companies tab's first row is {values[0] if values else '(empty)'}; it needs "
                              f"the headers company_name | website | careers_url (missing: {', '.join(missing)}).")
        if "careers_url" not in header:
            warn("no careers_url column (optional, but add it to paste careers pages for hard cases)")
        rows = values[1:6]
        ok(f"headers {values[0]}; first {len(rows)} row(s):")
        for r in rows:
            say(f"        {r}")
        if not rows:
            warn("the Companies tab has no companies yet")

        say("4. Write access (create, write, read back, delete a test tab)")
        test_title = f"_check_sheets_{datetime.now():%Y%m%d%H%M%S}"
        test_ws = None
        try:
            test_ws = spreadsheet.add_worksheet(title=test_title, rows=2, cols=2)
            test_ws.update(values=[["check-sheets", "ok"]], range_name="A1", value_input_option="RAW")
            back = test_ws.get_values("A1:B1")
            if back != [["check-sheets", "ok"]]:
                raise CheckFailed(f"Wrote a test value but read back {back!r}.")
        except CheckFailed:
            raise
        except Exception as e:  # noqa: BLE001
            raise _explain(e, "write a test tab", email, project) from e
        finally:
            if test_ws is not None:
                try:
                    spreadsheet.del_worksheet(test_ws)
                except Exception as e:  # noqa: BLE001
                    warn(f"could not delete the test tab '{test_title}' ({type(e).__name__}); delete it by hand")
        ok(f"wrote and deleted test tab '{test_title}'")

        say("5. Google Drive API (optional)")
        try:
            client.get_file_drive_metadata(config.SHEET_ID)
            ok("Drive API reachable")
        except Exception as e:  # noqa: BLE001
            code, status, message = _api_error_info(e)
            if _disabled_api(status, message):
                warn(f"Google Drive API is not enabled for project '{project}'. The scraper only needs the "
                     f"Sheets API, but enable Drive too to match the setup guide: "
                     f"https://console.cloud.google.com/apis/library/drive.googleapis.com?project={project}")
            else:
                warn(f"Drive API check failed ({type(e).__name__}); not needed by the scraper")
    except CheckFailed as e:
        say(f"  FAIL  {e}")
        say("\nSheets check FAILED.")
        return False
    say("\nSheets check passed: the scraper can read Companies and write its tabs.")
    return True

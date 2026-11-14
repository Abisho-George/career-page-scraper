"""Google Sheets in/out via gspread + a service account, plus a CSV reader for local runs."""
from __future__ import annotations

import csv
import logging
import re
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

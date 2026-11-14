"""SQLite store. SQLite holds the history; the Sheet is a view rebuilt from it after each run."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

STATUSES = ("OK", "BLOCKED", "ROBOTS_DISALLOWED", "NO_CAREERS_PAGE", "NO_JOBS_FOUND", "ERROR", "PENDING")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    website TEXT UNIQUE NOT NULL,
    careers_url TEXT,
    careers_url_source TEXT,          -- 'sheet' | 'discovered'
    ats TEXT,
    ats_token TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    status_reason TEXT,
    method TEXT,                      -- source_method of the last successful extraction
    job_count INTEGER DEFAULT 0,
    last_checked TEXT,
    last_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT,
    location TEXT,
    remote TEXT,
    department TEXT,
    employment_type TEXT,
    description TEXT,
    apply_url TEXT,
    posted_at TEXT,
    source_method TEXT,
    first_seen TEXT,
    last_seen TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id, is_active);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    companies_total INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    robots_disallowed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    no_jobs INTEGER DEFAULT 0,
    no_careers INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    jobs_closed INTEGER DEFAULT 0,
    ok_ats_api INTEGER DEFAULT 0,
    ok_jsonld INTEGER DEFAULT 0,
    ok_heuristic INTEGER DEFAULT 0,
    ok_browser INTEGER DEFAULT 0,
    top_no_jobs_ats TEXT
);
"""


# Columns added after the first release. connect() adds any that an older database lacks,
# so an existing data/jobs.db keeps working (ALTER TABLE ... ADD COLUMN never touches rows).
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "runs": [("robots_disallowed", "INTEGER DEFAULT 0")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


# ---------------------------------------------------------------- companies

def upsert_company(conn: sqlite3.Connection, name: str, website: str, careers_url: str = "") -> int:
    """Insert or update a company from the sheet. A careers_url typed in the sheet always wins;
    if it is later removed from the sheet we forget it so discovery runs again."""
    row = conn.execute("SELECT id, careers_url_source FROM companies WHERE website = ?", (website,)).fetchone()
    careers_url = (careers_url or "").strip()
    if row is None:
        cur = conn.execute(
            "INSERT INTO companies (name, website, careers_url, careers_url_source, status) "
            "VALUES (?, ?, ?, ?, 'PENDING')",
            (name, website, careers_url or None, "sheet" if careers_url else None),
        )
        return cur.lastrowid
    if careers_url:
        conn.execute(
            "UPDATE companies SET name = ?, careers_url = ?, careers_url_source = 'sheet' WHERE id = ?",
            (name, careers_url, row["id"]),
        )
    elif row["careers_url_source"] == "sheet":
        conn.execute(
            "UPDATE companies SET name = ?, careers_url = NULL, careers_url_source = NULL, "
            "ats = NULL, ats_token = NULL WHERE id = ?",
            (name, row["id"]),
        )
    else:
        conn.execute("UPDATE companies SET name = ? WHERE id = ?", (name, row["id"]))
    return row["id"]


def get_company(conn: sqlite3.Connection, website: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE website = ?", (website,)).fetchone()


def get_companies_to_process(conn: sqlite3.Connection, run_id: int, limit: int = 0,
                             websites: set[str] | None = None) -> list[sqlite3.Row]:
    """Companies not yet processed in this run, least recently checked first (so a capped run
    rotates through the whole list over several days)."""
    rows = conn.execute(
        "SELECT * FROM companies WHERE last_run_id IS NULL OR last_run_id != ? "
        "ORDER BY last_checked IS NOT NULL, last_checked, id",
        (run_id,),
    ).fetchall()
    if websites is not None:
        rows = [r for r in rows if r["website"] in websites]
    return rows[:limit] if limit else rows


def update_company(conn: sqlite3.Connection, company_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE companies SET {cols} WHERE id = ?", (*fields.values(), company_id))


# ---------------------------------------------------------------- jobs

JOB_FIELDS = ("title", "location", "remote", "department", "employment_type", "description",
              "apply_url", "posted_at", "source_method")


def upsert_job(conn: sqlite3.Connection, company_id: int, job_id: str, data: dict, seen_at: str) -> str:
    """Returns 'new' (never seen), 'reopened' (was closed) or 'seen' (still open)."""
    row = conn.execute("SELECT is_active FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    values = [data.get(f, "") or "" for f in JOB_FIELDS]
    if row is None:
        conn.execute(
            f"INSERT INTO jobs (job_id, company_id, {', '.join(JOB_FIELDS)}, first_seen, last_seen, is_active) "
            f"VALUES (?, ?, {', '.join('?' for _ in JOB_FIELDS)}, ?, ?, 1)",
            (job_id, company_id, *values, seen_at, seen_at),
        )
        return "new"
    # An empty value never overwrites a stored one (e.g. a Workday description fetched on an
    # earlier run is kept when this run skipped the detail call).
    sets = ", ".join(f"{f} = CASE WHEN ? = '' THEN {f} ELSE ? END" for f in JOB_FIELDS)
    params = [p for v in values for p in (v, v)]
    conn.execute(
        f"UPDATE jobs SET {sets}, last_seen = ?, is_active = 1, closed_at = NULL WHERE job_id = ?",
        (*params, seen_at, job_id),
    )
    return "seen" if row["is_active"] else "reopened"


def mark_seen(conn: sqlite3.Connection, company_id: int, seen_job_ids: set[str], closed_at: str) -> int:
    """Close this company's active jobs that were not seen in this pass. Returns how many closed."""
    active = conn.execute(
        "SELECT job_id FROM jobs WHERE company_id = ? AND is_active = 1", (company_id,)
    ).fetchall()
    to_close = [r["job_id"] for r in active if r["job_id"] not in seen_job_ids]
    conn.executemany(
        "UPDATE jobs SET is_active = 0, closed_at = ? WHERE job_id = ?",
        [(closed_at, j) for j in to_close],
    )
    return len(to_close)


def active_job_count(conn: sqlite3.Connection, company_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE company_id = ? AND is_active = 1", (company_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------- runs

def get_open_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def start_run(conn: sqlite3.Connection, fresh: bool = False) -> tuple[int, bool]:
    """Resume the last unfinished run unless fresh=True. Returns (run_id, resumed)."""
    open_run = get_open_run(conn)
    if open_run and not fresh:
        return open_run["id"], True
    if open_run and fresh:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (now_iso(), open_run["id"]))
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),))
    conn.commit()
    return cur.lastrowid, False


def add_run_counts(conn: sqlite3.Connection, run_id: int, jobs_new: int = 0, jobs_closed: int = 0) -> None:
    conn.execute(
        "UPDATE runs SET jobs_new = jobs_new + ?, jobs_closed = jobs_closed + ? WHERE id = ?",
        (jobs_new, jobs_closed, run_id),
    )


def finish_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    """Compute per-run totals from the companies processed in this run and close the run."""
    rows = conn.execute(
        "SELECT status, method, ats FROM companies WHERE last_run_id = ?", (run_id,)
    ).fetchall()
    count = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
    no_jobs_ats: dict[str, int] = {}
    for r in rows:
        if r["status"] == "NO_JOBS_FOUND":
            key = r["ats"] or "unknown"
            no_jobs_ats[key] = no_jobs_ats.get(key, 0) + 1
    top = sorted(no_jobs_ats.items(), key=lambda kv: -kv[1])[:5]
    conn.execute(
        """UPDATE runs SET finished_at = ?, companies_total = ?, ok = ?, blocked = ?, robots_disallowed = ?, failed = ?,
           no_jobs = ?, no_careers = ?, ok_ats_api = ?, ok_jsonld = ?, ok_heuristic = ?,
           ok_browser = ?, top_no_jobs_ats = ? WHERE id = ?""",
        (
            now_iso(), len(rows),
            count(lambda r: r["status"] == "OK"),
            count(lambda r: r["status"] == "BLOCKED"),
            count(lambda r: r["status"] == "ROBOTS_DISALLOWED"),
            count(lambda r: r["status"] == "ERROR"),
            count(lambda r: r["status"] == "NO_JOBS_FOUND"),
            count(lambda r: r["status"] == "NO_CAREERS_PAGE"),
            count(lambda r: r["status"] == "OK" and r["method"] == "ats_api"),
            count(lambda r: r["status"] == "OK" and r["method"] == "jsonld"),
            count(lambda r: r["status"] == "OK" and r["method"] == "heuristic"),
            count(lambda r: r["status"] == "OK" and r["method"] == "browser"),
            ", ".join(f"{k} ({v})" for k, v in top),
            run_id,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

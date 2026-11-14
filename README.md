# Career Page Job Scraper

A zero-cost Python agent that reads companies from a Google Sheet, finds each careers page, pulls
every open job and writes them back to the Sheet on a schedule. Sites with bot protection are
marked `BLOCKED` and skipped, never bypassed.

- **Input:** tab `Companies` with columns `company_name | website | careers_url` (careers_url optional).
- **Output:** tabs `Jobs` (active jobs + jobs closed in the last 7 days), `Company Status` (problems
  first: `ERROR`, `BLOCKED`, `ROBOTS_DISALLOWED`, `NO_CAREERS_PAGE`, `NO_JOBS_FOUND`, then `OK`) and
  `Runs` (one summary row per run, newest first, with a count per status).
- **History** lives in SQLite (`data/jobs.db`); the Sheet tabs are rebuilt from it after every run, so
  a failed Sheets write never loses data.

## How it works

Each company runs through these stages and stops at the first that returns jobs:

1. **Discover** the careers page: `careers_url` from the sheet, else probe `/careers`, `/jobs`,
   `/join-us`, … and `careers.` / `jobs.` subdomains, else the best careers link on the homepage.
2. **Block check** (`scraper/blockcheck.py`): Cloudflare, captcha walls, DataDome, PerimeterX,
   Akamai, Imperva, 403/429/503 → `BLOCKED`, no retry. A robots.txt disallow on the careers page or
   ATS endpoint → `ROBOTS_DISALLOWED`, also skipped.
3. **Detect the ATS** from the URL, links, iframes and scripts (Greenhouse, Lever, Ashby,
   SmartRecruiters, Workable, Recruitee, Workday; Darwinbox, Keka, Zoho Recruit, Freshteam, iCIMS,
   Taleo, SuccessFactors and BambooHR are recorded but not extracted yet). If the landing page has
   no ATS, one "View open positions" link is followed.
4. **Extract**: ATS public JSON API → schema.org `JobPosting` JSON-LD → heuristic job links
   (+ trafilatura) → headless Chromium render (only for JS-rendered pages), then JSON-LD/heuristic
   again on the rendered page.
5. **Normalize** (city aliases, remote/hybrid, ISO dates) and **diff** (new / still open / closed).
   Jobs are only closed when the company finished `OK`.

Once a company's ATS token is known, later runs go straight to the ATS API. If that board is
gone (HTTP 404/410 or a response that isn't a job board), or returns 0 jobs two runs in a row
while the company had jobs, the stored ATS is cleared and discovery + detection run again in the
same run; the reason starts with "ATS changed or removed, rediscovered". Jobs are never closed on
that run (the next successful run closes the old ones). If rediscovery lands on the same board and
it is still empty, the company is taken to have no openings and its old jobs are closed. Other ATS
failures (5xx, bad JSON) give `ERROR` and keep the stored token.

| Status | Meaning | What you do |
| --- | --- | --- |
| `OK` | jobs extracted (0 only if the ATS itself says 0) | nothing |
| `NO_JOBS_FOUND` | careers page found, nothing extractable | spot-check; may need a new ATS module |
| `NO_CAREERS_PAGE` | discovery failed | paste `careers_url` in the sheet once |
| `BLOCKED` | bot protection detected | ignore for the MVP |
| `ROBOTS_DISALLOWED` | the site's robots.txt asks bots not to fetch the careers page or ATS endpoint; the reason names the path | respect it; ask the company or drop it from the list |
| `ERROR` | unexpected exception / timeout; reason holds the type | check the logs if it repeats |

## One-time setup

1. **Python 3.11+** and Git.
2. **Google service account** (free): in Google Cloud Console create a project, enable the
   *Google Sheets API* and *Google Drive API*, create a service account and download a JSON key as
   `service_account.json` in this folder. It is git-ignored; never commit it.
3. **The Sheet:** one spreadsheet with a tab `Companies` and headers
   `company_name | website | careers_url`. Share it (Editor) with the service account's
   `…@….iam.gserviceaccount.com` address. Copy the sheet ID from its URL (between `/d/` and `/edit`).
4. **Install:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m playwright install chromium
   cp .env.example .env               # then set SHEET_ID and CONTACT_EMAIL
   ```

## Commands

```bash
python main.py run                    # full run (resumes an unfinished run first)
python main.py run --limit 20         # quick run
python main.py run --fresh            # start a new run even if the last one crashed
python main.py run --csv companies.csv --no-sheets   # local run without Google Sheets
python main.py company acme.com       # debug one company with a verbose trace
python main.py company acme.com --careers-url https://acme.com/about/jobs
python main.py sheets                 # rewrite the Sheet tabs from SQLite
pytest -q                             # offline tests (no network)
```

Every company logs one line: `domain | method | status | jobs | seconds | reason`
(also written to `logs/run-YYYYMMDD.log`).

**Crash resume:** progress is committed after every company. If a run is killed, the next
`python main.py run` continues the same run with only the remaining companies.

## Running unattended

### GitHub Actions (recommended)

`.github/workflows/scrape.yml` runs daily at 02:00 IST (20:30 UTC) and on a manual
"Run workflow" click (with optional `limit` and `fresh` inputs). Add these repository secrets
(Settings → Secrets and variables → Actions):

| Secret | Value |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the full contents of `service_account.json` |
| `SHEET_ID` | the sheet ID |
| `CONTACT_EMAIL` | the address put in the User-Agent |

Optional repository *variable* `MAX_COMPANIES_PER_RUN` caps each run (least recently checked
companies go first, so a capped daily run rotates through the whole list).

The database is carried between runs with `actions/cache` (restored at the start, saved at the end
even when the run fails) and also uploaded as a 30-day artifact. Download one every few weeks as a
backup. If a restore ever fails, the only loss is change history: the next run sees every job as new.

Free private-repo minutes are capped per month: start weekly (`30 20 * * 0`) or keep runs short
if you are near the limit. The job stops itself after 310 minutes so the database is always saved;
an unfinished run is resumed by the next one.

### Your own machine

Linux/macOS cron (daily 02:30 local time):

```cron
30 2 * * * cd ~/job-scraper && .venv/bin/python main.py run >> logs/cron.log 2>&1
```

Windows Task Scheduler: create a basic task, trigger *Daily*, action *Start a program* with program
`C:\path\to\job-scraper\.venv\Scripts\python.exe`, arguments `main.py run` and *Start in*
`C:\path\to\job-scraper`. Tick "Wake the computer to run this task" under Conditions.

## Configuration

All optional settings are in `.env.example`: delays (2–5 s per domain), timeouts, parallelism
(5 companies, 2 browser pages), `COMPANY_TIMEOUT` (hard cap per company),
`HEURISTIC_MAX_LINKS` (150), `MAX_DETAIL_FETCHES` (detail calls per company per run for Workday,
SmartRecruiters and Workable; each run spends the budget on new jobs first, then on stored jobs
whose description is still empty, oldest first, so a 500-job Workday tenant is fully described
after 3 runs and a failed detail call is retried later), `USE_BROWSER`, `BROWSER_EXECUTABLE`, and
`JOBS_TAB_CLOSED_DAYS`.

## Politeness and rules

- Honest User-Agent `JobScraperBot/0.1 (+mailto:CONTACT_EMAIL)`, robots.txt respected (RFC 9309:
  4xx = allowed, 5xx = disallowed).
- One request at a time per host with a random 2–5 s gap; at most 5 companies in parallel.
- Retries only for timeouts / connection errors (max 2), never for 403/429/503.
- No proxies, no stealth plugins, no CAPTCHA solving, no paid or AI APIs.

## Known limitations

- Custom careers pages without JSON-LD give rough locations/departments and can miss jobs behind
  filters or search boxes.
- Indian ATS platforms (Darwinbox, Keka, Zoho Recruit, Freshteam) are detected but not extracted:
  those companies show `NO_JOBS_FOUND` with the ATS name until a module exists. The `Runs` tab
  lists the top `NO_JOBS_FOUND` platforms to tell you which module to build next.
- Homepages that only render links with JavaScript can end as `NO_CAREERS_PAGE`; paste the
  `careers_url` in the sheet for those.
- `posted_at` is often empty outside ATS feeds; `first_seen` is the reliable "new job" date.
- For pages scraped without an ATS, if a company suddenly shows fewer than half of its previous
  jobs (and had at least 10), nothing is closed that run and the reason says
  "sharp drop held, waiting one run" (the new count is kept in `companies.pending_drop_count`).
  If the next run sees about the same count (within ±20%), the drop is accepted and the missing
  jobs are closed; if the count recovered, the hold is cleared. This stops a site redesign from
  closing every job at once without keeping stale jobs open forever.

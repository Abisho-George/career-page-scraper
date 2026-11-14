# Live test checklist

Everything so far was tested offline against hand-written fixtures. This checklist runs the
scraper against the real Google Sheet, real ATS APIs and real careers pages on your machine, and
turns what it sees into offline tests. Tick each box as you go.

Allow about 2 hours. Steps 1–3 take about 30 minutes, the 30-company run up to an hour, and the
two GitHub runs as long as each takes.

---

## 0. Setup (once)

- [ ] Python 3.11 or newer: `python --version`
- [ ] Get the code and the branch:

  ```bash
  git clone https://github.com/Abisho-George/career-page-scraper.git job-scraper
  cd job-scraper
  git checkout claude/new-session-bfc3dz
  ```

- [ ] Install (Windows: `.venv\Scripts\activate` instead of `source …`):

  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m playwright install chromium
  ```

- [ ] `cp .env.example .env`, then set `SHEET_ID` and `CONTACT_EMAIL` in `.env`.
- [ ] Put the service-account key in the project folder as `service_account.json` (it is git-ignored).
- [ ] `pytest -q` is green (1 test skipped until step 3 adds a recording; the real-browser test
      runs once Chromium is installed).

## 1. Check the Google Sheet connection

```bash
python main.py check-sheets
```

- [ ] It ends with `Sheets check passed`. It printed the sheet title, every tab, the
      `Companies` headers and its first 5 rows, then created and deleted a `_check_sheets_…` tab.

If it fails, the `FAIL` line says what to do. The usual ones:

| Message | Fix |
| --- | --- |
| `SHEET_ID is empty` | copy the ID from the sheet URL (between `/d/` and `/edit`) into `.env` |
| `Service account key not found` | save the JSON key as `service_account.json`, or set `GOOGLE_APPLICATION_CREDENTIALS` |
| `Google Sheets API is not enabled …` | open the printed link, click **Enable**, wait a minute (enable the Drive API too) |
| `the sheet is not shared with the service account` | Share the sheet with the `…iam.gserviceaccount.com` address as **Editor** |
| `can read the sheet but not edit it` | change that address from Viewer to **Editor** |
| `No tab named 'Companies'` / `missing: company_name` | first row of `Companies` must be `company_name | website | careers_url` |

A `WARN` line (e.g. Drive API not enabled, CONTACT_EMAIL unset) doesn't stop the scraper, but fix
CONTACT_EMAIL before scraping: it goes in the User-Agent.

## 2. One company per ATS, saving every response

Pick one company per platform from your 30-company test list (the careers page tells you: the job
links go to `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`,
`jobs.smartrecruiters.com`, `apply.workable.com`, `*.recruitee.com`, `*.myworkdayjobs.com`).
Also pick one custom page with JSON-LD, one custom page without, one JavaScript-only page and one
big brand that is likely bot-protected.

For each:

```bash
python main.py company <domain> --save-fixtures
```

For big Workday or SmartRecruiters companies, cap the detail calls so the recording stays small
(the job count is unaffected):

```bash
MAX_DETAIL_FETCHES=5 python main.py company <domain> --save-fixtures
```

(Windows PowerShell: `$env:MAX_DETAIL_FETCHES=5; python main.py company <domain> --save-fixtures`)

This always starts from a clean state (discovery included) in a throwaway database, so
`data/jobs.db` is not changed. It writes `tests/fixtures/live/<domain>/`:

- `001_GET_<host>_<path>.html|json|txt`: every raw response, in request order (robots.txt and redirect hops included)
- `NNN_GET-ERROR_…`: a request that got no response (DNS, timeout); `NNN_RENDERED_…html`: a browser-rendered page
- `index.json`: URL, status, headers and request body for each file
- `summary.json`: `status`, `reason`, `ats`, `ats_token`, `method`, `job_count`, `sample_jobs`

Tick each when its `summary.json` and the live careers page agree:

| | Platform | Domain | status / method | job_count = careers page ±1 | 2 jobs spot-checked (title, apply URL opens, location) |
| --- | --- | --- | --- | --- | --- |
| [ ] | Greenhouse | | | | |
| [ ] | Lever | | | | |
| [ ] | Ashby | | | | |
| [ ] | SmartRecruiters | | | | |
| [ ] | Workable | | | | |
| [ ] | Recruitee | | | | |
| [ ] | Workday | | | | |
| [ ] | Custom with JSON-LD | | `jsonld` | | |
| [ ] | Custom without JSON-LD | | `heuristic` | | |
| [ ] | JavaScript-only | | `browser` | | |
| [ ] | Bot-protected brand | | `BLOCKED` + reason | n/a | n/a |

If an ATS gives the wrong count or broken fields, open its saved API response (e.g.
`003_GET_boards-api.greenhouse.io_v1_boards_<token>_jobs_content-true.json`) and compare it with
the mapper in `scraper/ats/<ats>.py`. The hand-written fixtures followed each API's documented shape,
so a difference here is exactly what this step is meant to catch. Workday and Workable are the
most likely to need a fix.

## 3. Turn the saved responses into tests

`tests/fixtures/live/` is git-ignored, so nothing is committed by accident. Promote what you want
to keep:

**a) Whole-company replay test (recommended, no code to write).** Copy the folder into
`tests/fixtures/replay/`:

```bash
mkdir -p tests/fixtures/replay
cp -r tests/fixtures/live/<domain> tests/fixtures/replay/
pytest -q tests/test_replay.py
```

`tests/test_replay.py` replays every folder there offline (saved responses only, anything else is
a 404) and checks that status, ATS, token, method and job count match `summary.json`. If you later
change the scraper and a real site's behaviour breaks, this test says so.

**b) Real API response for a mapper test.** Replace the hand-written fixture with the real one and
update the expected values in `tests/test_ats.py`:

```bash
cp "tests/fixtures/live/<domain>/003_GET_boards-api.greenhouse.io_v1_boards_<token>_jobs_content-true.json" tests/fixtures/greenhouse_jobs.json
pytest -q tests/test_ats.py
```

- [ ] One replay folder per ATS (plus the custom and JS pages) in `tests/fixtures/replay/`
- [ ] `pytest -q` green
- [ ] Glance through the copied files for anything you don't want in the repo (they are public
      job pages, but response headers can include cookies), then commit:

```bash
git add tests/fixtures/replay tests/fixtures/*.json
git commit -m "Add live recordings as replay tests"
```

## 4. Five companies, end to end

```bash
python main.py run --limit 5
```

- [ ] The last line is a summary (`Run N: 5 companies | OK … | BLOCKED … | ROBOTS_DISALLOWED … |
      …`) with no `SHEETS NOT UPDATED`.
- [ ] The Sheet now has `Jobs`, `Company Status` and `Runs` tabs. Problems are listed first in
      Company Status, and every company has a status and a reason.
- [ ] `logs/run-YYYYMMDD.log` has one line per company: `domain | method | status | jobs | seconds | reason`.

## 5. All 30 test companies

```bash
python main.py run --limit 30
```

It picks the companies least recently checked first, so these are the 25 not yet processed
plus the earliest 5 again. Then run the weekly checklist from the build doc:

- [ ] every company has a status and a reason (no blanks, no `PENDING`)
- [ ] 10 random `OK` companies: job count matches the live careers page ±1
- [ ] for those 10, 2 jobs each: correct title, working apply URL, plausible location
- [ ] bot-protected brands show `BLOCKED` with a specific reason, never `ERROR`
- [ ] robots.txt refusals show `ROBOTS_DISALLOWED` (not `BLOCKED`)
- [ ] the log shows 2–5 s between requests to the same domain (no bursts)
- [ ] at least 80% of non-blocked companies end `OK` or a correct `NO_JOBS_FOUND`

For any company that looks wrong: `python main.py company <domain> --save-fixtures`, inspect,
fix, then add it to `tests/fixtures/replay/` so it stays fixed.

## 6. Two manual GitHub runs

Scheduled workflows and the **Run workflow** button only exist for workflow files on the
repository's **default branch**, so merge this branch first (or open a PR and merge it).

- [ ] Repository → Settings → Secrets and variables → Actions → **New repository secret**:
      `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole key file), `SHEET_ID`, `CONTACT_EMAIL`.
- [ ] Actions → **scrape** → **Run workflow** (leave `limit` 0 for all, or 30 to match step 5).
      When it finishes:
  - [ ] the job is green, and the Sheet's `Runs` tab has a new top row
  - [ ] the run has a `jobs-db-…` artifact (the database backup)
  - [ ] this first GitHub run starts from an empty database (your local `data/jobs.db` is not
        uploaded), so every job counts as new; that's expected
- [ ] **Run workflow** again with the same `limit`. When it finishes:
  - [ ] the **Restore database** step says the cache was restored (`Cache restored from key: jobs-db-…`)
  - [ ] the new top row of `Runs` shows **jobs_new = 0 and jobs_closed = 0**

If the second run is not 0 / 0: a few new or closed jobs can be real (a company posted or
removed a role in between). Many new jobs mean the database wasn't restored (check the Restore
step). Many closed jobs at one company mean its page changed: run
`python main.py company <domain> --save-fixtures` locally and look at what it sees.

When both runs look right, the daily schedule (02:00 IST) takes over. To start weekly instead,
change the cron line in `.github/workflows/scrape.yml` to `30 20 * * 0`.

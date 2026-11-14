# Career Page Job Scraper

Zero-cost Python agent: reads companies from a Google Sheet, finds each
careers page, extracts open jobs, writes Jobs + Company Status back to the Sheet.

## Hard rules
- Zero cost: no paid APIs, no LLM APIs, no proxies, no paid search.
- Never bypass bot protection. If blockcheck.py says blocked, set
  status=BLOCKED with a reason and move on. No retries, no stealth plugins,
  no CAPTCHA solving, no rotating user agents.
- Respect robots.txt. Honest User-Agent: "JobScraperBot/0.1 (+mailto:CONTACT_EMAIL)".
- Politeness: max 1 concurrent request per domain, 2-5 s random delay between
  requests to the same domain, max 5 companies in parallel.
- One company failing must never stop the run. Wrap each company in
  try/except, record status=ERROR + reason, continue.
- Secrets only from .env / environment. Never commit service_account.json.

## Stack
Python 3.11+, httpx, selectolax, trafilatura, playwright (chromium),
gspread + google-auth, sqlite3 (stdlib), python-dotenv, pytest, tenacity.

## Extraction ladder (stop at first that returns jobs)
1. ATS public JSON API (scraper/ats/*)
2. schema.org JobPosting JSON-LD (extract/jsonld.py)
3. Heuristic job links + trafilatura (extract/heuristic.py)
4. Playwright render, then repeat 2 and 3 (browser.py)

## Conventions
- Every ATS module exposes: async fetch_jobs(token: str, fetcher, plan: DetailPlan | None = None) -> list[Job]
  (async because the whole pipeline runs on asyncio; `fetcher` is the shared polite client).
  Modules that need one call per job for the description (Workday, SmartRecruiters, Workable v3)
  call details only for `plan.pick(jobs)`: new jobs first, then stored jobs with an empty
  description, oldest first_seen first, capped at MAX_DETAIL_FETCHES (200) per company per run.
- Job is a dataclass in scraper/ats/base.py; all extractors return it.
- Tests use saved fixtures in tests/fixtures; no live network in pytest
  (use httpx.MockTransport via tests/helpers.py). Recordings copied to tests/fixtures/replay/<domain>/
  are replayed by tests/test_replay.py (helpers.load_recording).
- Log one line per company: domain | method | status | jobs | seconds.
- Raise BlockedError for bot protection and RobotsDisallowedError (its subclass) for robots.txt;
  set the company status from `e.status`, never hard-code it.

## Company statuses
| Status | Meaning |
| --- | --- |
| OK | jobs extracted (0 only if the ATS itself says 0) |
| NO_JOBS_FOUND | careers page found, nothing extractable |
| NO_CAREERS_PAGE | discovery failed |
| BLOCKED | bot-protection signal only (blockcheck.py, or 403/429/503 from an ATS API) |
| ROBOTS_DISALLOWED | robots.txt disallows the careers page or the ATS endpoint; reason keeps the text, e.g. "robots.txt disallows /careers". A robots.txt that fails with a plain 5xx is treated as disallow-all (RFC 9309) with reason "robots.txt unavailable (http 500); treated as disallow"; a bot-protection page served on robots.txt is BLOCKED |
| ERROR | unexpected exception / timeout; reason holds the type |
| PENDING | not processed yet |

## Commands
- python main.py run                 # full run (resumes an unfinished run)
- python main.py run --limit 20      # quick run
- python main.py run --fresh         # start a new run even if the last one crashed
- python main.py run --csv companies.csv --no-sheets   # local run without Google Sheets
- python main.py company acme.com    # debug one company, verbose
- python main.py company acme.com --save-fixtures   # record raw responses to tests/fixtures/live/acme.com/
- python main.py check-sheets        # verify Sheets setup (.env, key, sharing, APIs, write access)
- python main.py sheets              # rebuild the Sheet tabs from SQLite only
- pytest -q

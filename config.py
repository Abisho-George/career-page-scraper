"""Central configuration. Everything secret or deployment-specific comes from .env / environment."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


SHEET_ID = os.getenv("SHEET_ID", "")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "") or "unset@example.com"
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", str(ROOT / "service_account.json"))
MAX_COMPANIES_PER_RUN = _int("MAX_COMPANIES_PER_RUN", 0)

DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "jobs.db")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(ROOT / "logs")))

USER_AGENT = f"JobScraperBot/0.1 (+mailto:{CONTACT_EMAIL})"
ROBOTS_AGENT = "JobScraperBot"

# Politeness
MIN_DELAY = _float("MIN_DELAY", 2.0)
MAX_DELAY = _float("MAX_DELAY", 5.0)
REQUEST_TIMEOUT = _float("REQUEST_TIMEOUT", 20.0)
MAX_RETRIES = 2  # only for timeouts / connection errors
MAX_PARALLEL_COMPANIES = _int("MAX_PARALLEL_COMPANIES", 5)
MAX_BROWSER_PAGES = _int("MAX_BROWSER_PAGES", 2)
COMPANY_TIMEOUT = _float("COMPANY_TIMEOUT", 900.0)  # hard cap per company, seconds
MAX_BODY_BYTES = 5_000_000

# Extraction limits
HEURISTIC_MAX_LINKS = _int("HEURISTIC_MAX_LINKS", 150)
HEURISTIC_MAX_PAGES = 10
MAX_DETAIL_FETCHES = _int("MAX_DETAIL_FETCHES", 200)  # per company, for ATS detail calls
DESCRIPTION_MAX_CHARS = 20_000
SHEET_DESCRIPTION_CHARS = 500
JOBS_TAB_CLOSED_DAYS = _int("JOBS_TAB_CLOSED_DAYS", 7)

# Browser
USE_BROWSER = os.getenv("USE_BROWSER", "1") not in ("0", "false", "no")
BROWSER_EXECUTABLE = os.getenv("BROWSER_EXECUTABLE", "")
BROWSER_TIMEOUT_MS = 20_000

# Change tracking: for non-ATS methods, don't close jobs if we suddenly see far fewer
# than before (protects against a page redesign closing everything at once). The drop is held
# for one run; if the next run sees about the same lower count (within the tolerance) it is
# accepted and the missing jobs are closed.
CLOSE_GUARD_MIN_PREVIOUS = 10
CLOSE_GUARD_RATIO = 0.5
DROP_CONFIRM_TOLERANCE = 0.2

# Careers discovery
CAREERS_PATHS = [
    "/careers", "/careers/", "/jobs", "/join-us", "/work-with-us",
    "/about/careers", "/company/careers",
]
CAREERS_SUBDOMAINS = ["careers", "jobs"]

CITY_ALIASES = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "bombay": "Mumbai",
    "gurgaon": "Gurugram",
    "calcutta": "Kolkata",
    "madras": "Chennai",
    "poona": "Pune",
    "trivandrum": "Thiruvananthapuram",
    "new delhi": "New Delhi",
    "delhi ncr": "Delhi NCR",
    "nyc": "New York",
    "new york city": "New York",
    "sf": "San Francisco",
    "sfo": "San Francisco",
    "bay area": "San Francisco Bay Area",
    "la": "Los Angeles",
}

# Used by the heuristic extractor to guess a location when there is no "Location:" label.
KNOWN_CITIES = [
    "Bengaluru", "Bangalore", "Mumbai", "Delhi", "New Delhi", "Gurugram", "Gurgaon", "Noida",
    "Hyderabad", "Chennai", "Pune", "Kolkata", "Ahmedabad", "Jaipur", "Kochi", "Chandigarh",
    "Indore", "Coimbatore", "London", "Dublin", "Berlin", "Paris", "Amsterdam", "Singapore",
    "Dubai", "Sydney", "Melbourne", "Toronto", "Vancouver", "New York", "San Francisco",
    "Seattle", "Austin", "Boston", "Chicago", "Los Angeles", "Denver", "Atlanta", "Remote",
]

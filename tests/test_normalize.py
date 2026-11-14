from datetime import date

from scraper.ats.base import Job, html_to_text
from scraper.normalize import clean_location, detect_remote, normalize_job, parse_date

TODAY = date(2026, 11, 14)


def test_city_aliases():
    assert clean_location("Bangalore, India") == "Bengaluru, India"
    assert clean_location("  Gurgaon /  Bombay ") == "Gurugram / Mumbai"
    assert clean_location("NYC") == "New York"
    assert clean_location("Los Angeles, CA") == "Los Angeles, CA"
    assert clean_location("SF") == "San Francisco"
    assert clean_location("La Jolla") == "La Jolla"


def test_remote_detection():
    assert detect_remote("Engineer (Remote)", "", "") == "remote"
    assert detect_remote("Engineer", "Mumbai - Hybrid", "") == "hybrid"
    assert detect_remote("Engineer", "Mumbai", "This is a fully remote role.") == "remote"
    assert detect_remote("Engineer", "Mumbai", "We sell remote controls.") == ""
    assert detect_remote("Engineer", "Mumbai", "", current="onsite") == "onsite"


def test_dates():
    assert parse_date("2026-10-20T09:00:00-04:00", TODAY) == "2026-10-20"
    assert parse_date("Posted 3 Days Ago", TODAY) == "2026-11-11"
    assert parse_date("Posted 30+ Days Ago", TODAY) == "2026-10-15"
    assert parse_date("Posted Today", TODAY) == "2026-11-14"
    assert parse_date("Posted Yesterday", TODAY) == "2026-11-13"
    assert parse_date(1761900000000, TODAY) == "2025-10-31"
    assert parse_date("2026-10-30 10:00:00 UTC", TODAY) == "2026-10-30"
    assert parse_date("not a date", TODAY) == ""
    assert parse_date("", TODAY) == ""


def test_normalize_job():
    j = normalize_job(Job(title="  Sales\n Lead ", apply_url=" https://x.com/j ", location="Bangalore",
                          posted_at="Posted Today", description="x" * 30000), TODAY)
    assert j.title == "Sales Lead" and j.location == "Bengaluru" and j.posted_at == "2026-11-14"
    assert len(j.description) == 20000 and j.apply_url == "https://x.com/j"


def test_html_to_text():
    assert html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"
    assert html_to_text("<p>One</p><ul><li>Two</li><li>Three</li></ul><script>x()</script>") == "One\n\nTwo\n\nThree"

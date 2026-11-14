import pytest

from scraper.detect import ats_job_board_url, detect_ats
from tests.helpers import fixture


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
    ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
    ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
    ("https://jobs.lever.co/hooli/abc-123", ("lever", "hooli")),
    ("https://jobs.eu.lever.co/hooli", ("lever", "eu/hooli")),
    ("https://jobs.ashbyhq.com/ramp", ("ashby", "ramp")),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/X_R1", ("workday", "acme|wd5|External")),
    ("https://acme.wd1.myworkdayjobs.com/Careers", ("workday", "acme|wd1|Careers")),
    ("https://wd3.myworkdaysite.com/en-US/recruiting/acme/Jobs", ("workday", "acme|wd3|Jobs|site")),
    ("https://jobs.smartrecruiters.com/Visa1/7440000", ("smartrecruiters", "Visa1")),
    ("https://careers.smartrecruiters.com/Acme", ("smartrecruiters", "Acme")),
    ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme")),
    ("https://acme.recruitee.com/o/dev", ("recruitee", "acme")),
    ("https://tata.darwinbox.in/ms/candidate/careers", ("darwinbox", "tata")),
    ("https://acme.keka.com/careers", ("keka", "acme")),
    ("https://careers-acme.icims.com/jobs", ("icims", "careers-acme")),
])
def test_url_patterns(url, expected):
    assert detect_ats(url, "") == expected


def test_workable_job_link_without_account_is_ignored():
    assert detect_ats("https://apply.workable.com/j/ABC123", "") == ("unknown", "")


def test_greenhouse_embed_script_in_page():
    assert detect_ats("https://acme.com/careers", fixture("careers_greenhouse_embed.html")) == ("greenhouse", "acme")


def test_workday_link_in_page():
    assert detect_ats("https://acme.com/careers", fixture("careers_workday_link.html")) == ("workday", "acme|wd5|External")


def test_flag_only_ats_in_page():
    assert detect_ats("https://tata.com/careers", fixture("careers_darwinbox.html")) == ("darwinbox", "tataexample")


def test_custom_page():
    assert detect_ats("https://acme.com/careers", fixture("normal_careers.html")) == ("custom", "")


def test_label_for_is_not_a_greenhouse_token():
    html = '<div id="grnhse_x"></div><label for="email">Email</label>'
    assert detect_ats("https://acme.com/careers", html) == ("custom", "")


def test_board_urls():
    assert ats_job_board_url("workday", "acme|wd5|External") == "https://acme.wd5.myworkdayjobs.com/External"
    assert ats_job_board_url("lever", "eu/hooli") == "https://jobs.eu.lever.co/hooli"

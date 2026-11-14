from scraper.blockcheck import is_blocked
from scraper.fetch import Response
from tests.helpers import fixture


def resp(text, status=200, headers=None):
    return Response(url="https://x.com/careers", status=status, headers=headers or {}, text=text)


def test_cloudflare_challenge():
    assert is_blocked(resp(fixture("cloudflare_challenge.html"), 403)) == (True, "cloudflare challenge")
    # even when served with 200
    assert is_blocked(resp(fixture("cloudflare_challenge.html"), 200)) == (True, "cloudflare challenge")


def test_cf_mitigated_header():
    assert is_blocked(resp("<html></html>", 403, {"cf-mitigated": "challenge"})) == (True, "cloudflare challenge")


def test_normal_page_with_recaptcha_and_datadome_tag_is_not_blocked():
    assert is_blocked(resp(fixture("normal_careers.html"))) == (False, "")


def test_plain_403():
    assert is_blocked(resp(fixture("forbidden_403.html"), 403)) == (True, "http 403")


def test_429_and_503():
    assert is_blocked(resp("<html><body>slow down</body></html>", 429)) == (True, "http 429")
    assert is_blocked(resp("<html><body>busy</body></html>", 503)) == (True, "http 503")


def test_akamai():
    assert is_blocked(resp(fixture("akamai_denied.html"), 403)) == (True, "akamai")


def test_imperva():
    html = '<html><head><META NAME="robots" CONTENT="noindex"></head><body><iframe src="/_Incapsula_Resource?SWUDNSAI=31"></iframe></body></html>'
    assert is_blocked(resp(html)) == (True, "imperva")


def test_datadome_on_error():
    html = '<html><body><script src="https://ct.captcha-delivery.com/c.js"></script></body></html>'
    assert is_blocked(resp(html, 403, {"x-datadome": "protected"})) == (True, "datadome")


def test_perimeterx():
    html = '<html><body><div id="px-captcha"></div><p>Press &amp; Hold to confirm you are a human</p></body></html>'
    assert is_blocked(resp(html)) == (True, "perimeterx")


def test_thin_captcha_page():
    html = '<html><body><h1>Please verify you are human</h1><div class="h-captcha" data-sitekey="x"></div></body></html>'
    assert is_blocked(resp(html)) == (True, "captcha")


def test_suspected_challenge():
    html = "<html><head><script>window.location.href='/verify?t=1'</script></head><body></body></html>"
    assert is_blocked(resp(html)) == (True, "suspected challenge")


def test_404_is_not_blocked():
    assert is_blocked(resp(fixture("soft_404.html"), 404)) == (False, "")

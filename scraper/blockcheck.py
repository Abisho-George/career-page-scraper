"""Bot-protection detection. If this says blocked, the company is marked BLOCKED and skipped.

Signals are split in two:
- strong: only ever appear on a challenge / denial page, so they block on their own;
- weak:   vendor scripts, cookies or captcha widgets that many *normal* pages also carry
          (e.g. a reCAPTCHA on a contact form, the DataDome tag, the incap_ses cookie). They
          only name the vendor when the response is already an error, or when the page is a thin
          "prove you are human" page. This keeps normal careers pages from being marked BLOCKED.
"""
from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from scraper.fetch import Response

BLOCK_STATUSES = (403, 429, 503)

CF_TITLES = ("just a moment", "checking your browser", "attention required")
HUMAN_PHRASES = (
    "verify you are human", "verify that you are human", "are you a robot", "not a robot",
    "complete the security check", "unusual traffic", "press & hold", "press and hold",
    "enable javascript and cookies to continue", "checking if the site connection is secure",
)
REFRESH_SCRIPT = re.compile(
    r"(http-equiv=[\"']?refresh|location\.(href|replace|reload)|window\.location\s*=|document\.location)", re.I
)


def _page_facts(html: str) -> tuple[str, str, int]:
    """(title, visible text, number of links)"""
    if not html:
        return "", "", 0
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    links = len(tree.css("a[href]"))
    tree.strip_tags(["script", "style", "noscript", "template"])
    body = tree.body
    text = body.text(separator=" ", strip=True) if body else ""
    return title, text, links


def _strong(resp: Response, html_l: str, title_l: str, text_l: str, thin: bool) -> str | None:
    h = resp.headers
    cookies = h.get("set-cookie", "").lower()
    if h.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare challenge"
    if any(t in title_l for t in CF_TITLES):
        return "cloudflare challenge"
    if thin and any(t in text_l for t in CF_TITLES):
        return "cloudflare challenge"
    if "_cf_chl_opt" in html_l or "cf-chl-" in html_l or "cf_chl_" in html_l:
        return "cloudflare challenge"
    if "captcha-delivery.com" in html_l or ("x-datadome" in h and resp.status >= 400):
        return "datadome"
    if "px-captcha" in html_l or "press & hold" in text_l:
        return "perimeterx"
    if ("access denied" in title_l or "access denied" in text_l[:500]) and "reference #" in text_l \
            and ("edgesuite" in html_l or "akamai" in html_l):
        return "akamai"
    if "_incapsula_resource" in html_l or "incapsula incident id" in text_l:
        return "imperva"
    if "datadome" in cookies and resp.status >= 400:
        return "datadome"
    return None


def _weak(resp: Response, html_l: str) -> str | None:
    cookies = resp.headers.get("set-cookie", "").lower()
    if "challenges.cloudflare.com" in html_l and "turnstile" not in html_l:
        return "cloudflare challenge"
    if "cf-turnstile" in html_l or "challenges.cloudflare.com/turnstile" in html_l:
        return "captcha"
    if "g-recaptcha" in html_l or "google.com/recaptcha" in html_l or "recaptcha/api.js" in html_l:
        return "captcha"
    if "h-captcha" in html_l or "hcaptcha.com" in html_l:
        return "captcha"
    if "datadome" in html_l or "datadome" in cookies or "x-datadome" in resp.headers:
        return "datadome"
    if re.search(r"/_px\w*/|_pxappid|px-cdn\.net|perimeterx", html_l):
        return "perimeterx"
    if "incap_ses" in cookies or "visid_incap" in cookies:
        return "imperva"
    if "akamai" in html_l and "access denied" in html_l:
        return "akamai"
    return None


def is_blocked(resp: Response) -> tuple[bool, str]:
    html = resp.text or ""
    html_l = html.lower()
    title, text, links = _page_facts(html)
    title_l, text_l = title.lower(), text.lower()
    thin = len(text) < 1500 and links < 10

    reason = _strong(resp, html_l, title_l, text_l, thin)
    if reason:
        return True, reason

    human_check = thin and any(p in text_l for p in HUMAN_PHRASES)
    if resp.status in BLOCK_STATUSES or human_check:
        vendor = _weak(resp, html_l)
        if vendor:
            return True, vendor
        if resp.status in BLOCK_STATUSES:
            return True, f"http {resp.status}"
        return True, "captcha"

    if len(html.encode("utf-8", "ignore")) < 2048 and links == 0 and REFRESH_SCRIPT.search(html):
        return True, "suspected challenge"
    return False, ""

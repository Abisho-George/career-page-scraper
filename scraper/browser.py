"""Playwright fallback for JavaScript-rendered careers pages. Started lazily, only when needed."""
from __future__ import annotations

import asyncio
import logging
import re

from selectolax.lexbor import LexborHTMLParser

import config
from scraper.fetch import Response

log = logging.getLogger(__name__)

LOAD_MORE = re.compile(r"^\s*(load|show|view|see)\s+(more|all)(\s+(jobs|positions|openings|roles|results))?\s*$", re.I)
BLOCKED_RESOURCES = {"image", "font", "media"}
ROOT_SELECTORS = ("#root", "#__next", "#app", "#___gatsby", "[ng-version]", "#__nuxt", "[data-reactroot]")


def looks_js_rendered(html: str) -> bool:
    """Very little visible body text, or an SPA root container."""
    if not html:
        return True
    tree = LexborHTMLParser(html)
    has_root = any(tree.css_first(sel) is not None for sel in ROOT_SELECTORS)
    tree.strip_tags(["script", "style", "noscript", "template", "svg"])
    text = tree.body.text(separator=" ", strip=True) if tree.body else ""
    return len(text) < 500 or has_root


class Browser:
    def __init__(self, max_pages: int = config.MAX_BROWSER_PAGES):
        self._sem = asyncio.Semaphore(max_pages)
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None

    async def _ensure(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                kwargs = {"headless": True}
                if config.BROWSER_EXECUTABLE:
                    kwargs["executable_path"] = config.BROWSER_EXECUTABLE
                self._browser = await self._pw.chromium.launch(**kwargs)
        return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def render(self, url: str) -> Response:
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout

        browser = await self._ensure()
        async with self._sem:
            context = await browser.new_context(user_agent=config.USER_AGENT, java_script_enabled=True)
            try:
                page = await context.new_page()

                async def _route(route):
                    if route.request.resource_type in BLOCKED_RESOURCES:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", _route)
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=config.BROWSER_TIMEOUT_MS)
                try:
                    await page.wait_for_load_state("networkidle", timeout=config.BROWSER_TIMEOUT_MS)
                except PWTimeout:
                    pass
                for _ in range(10):
                    button = page.get_by_role("button", name=LOAD_MORE).or_(page.get_by_role("link", name=LOAD_MORE))
                    try:
                        if await button.count() == 0 or not await button.first.is_visible():
                            break
                        await button.first.click(timeout=5000)
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except (PWTimeout, PWError):
                        break
                html = await page.content()
                status = resp.status if resp else 200
                headers = {k.lower(): v for k, v in (await resp.all_headers()).items()} if resp else {}
                return Response(url=page.url, status=status, headers=headers, text=html)
            finally:
                await context.close()

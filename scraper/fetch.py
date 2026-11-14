"""Polite shared HTTP client.

- honest User-Agent, 20 s timeout, follows redirects
- max 1 in-flight request per host + 2-5 s random gap between requests to the same host
- robots.txt checked (and cached) per host before every request
- retries ONLY timeouts / connection errors (max 2), never 403/429/503
"""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_fixed

import config
from scraper.ats.base import BlockedError

log = logging.getLogger(__name__)

RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)


@dataclass
class Response:
    url: str
    status: int
    headers: dict = field(default_factory=dict)  # lower-cased keys; set-cookie values joined by "\n"
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return jsonlib.loads(self.text)


class Fetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None,
                 min_delay: float = config.MIN_DELAY, max_delay: float = config.MAX_DELAY,
                 respect_robots: bool = True, timeout: float = config.REQUEST_TIMEOUT):
        self.min_delay, self.max_delay = min_delay, max_delay
        self.respect_robots = respect_robots
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            max_redirects=10,
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en;q=0.9",
            },
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self.request_log: list[tuple[str, float]] = []  # (host, monotonic time) for tests/diagnostics

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ public API

    async def get(self, url: str, *, headers: dict | None = None, check_robots: bool = True) -> Response:
        return await self.request("GET", url, headers=headers, check_robots=check_robots)

    async def post_json(self, url: str, payload: dict, *, check_robots: bool = True) -> Response:
        return await self.request(
            "POST", url, content=jsonlib.dumps(payload),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            check_robots=check_robots,
        )

    async def request(self, method: str, url: str, *, headers: dict | None = None,
                      content: str | None = None, check_robots: bool = True) -> Response:
        if check_robots and self.respect_robots and not await self.allowed(url):
            path = urlsplit(url).path or "/"
            raise BlockedError(f"robots.txt disallows {path}")
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE),
            stop=stop_after_attempt(config.MAX_RETRIES + 1),
            wait=wait_fixed(1),
            reraise=True,
        ):
            with attempt:
                return await self._throttled(method, url, headers=headers, content=content)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._robots:
            lock = self._robots_locks.setdefault(key, asyncio.Lock())
            async with lock:
                if key not in self._robots:
                    self._robots[key] = await self._load_robots(key)
        parser = self._robots[key]
        if parser is None:
            return True
        return parser.can_fetch(config.ROBOTS_AGENT, url)

    # ------------------------------------------------------------ internals

    async def _load_robots(self, base: str) -> RobotFileParser | None:
        """RFC 9309: 2xx -> parse, 4xx -> allow all, 5xx -> disallow all.
        Network errors -> allow (the real request will fail on its own)."""
        try:
            r = await self._throttled("GET", f"{base}/robots.txt")
        except Exception as e:  # noqa: BLE001
            log.debug("robots.txt for %s unavailable: %s", base, e)
            return None
        parser = RobotFileParser()
        if r.ok and "html" not in r.headers.get("content-type", ""):
            parser.parse(r.text.splitlines())
            return parser
        if 400 <= r.status < 500 or r.ok:
            return None
        parser.disallow_all = True
        return parser

    async def _throttled(self, method: str, url: str, *, headers: dict | None = None,
                         content: str | None = None) -> Response:
        host = urlsplit(url).hostname or ""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                gap = random.uniform(self.min_delay, self.max_delay)
                wait = last + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self.request_log.append((host, time.monotonic()))
            try:
                r = await self._client.request(method, url, headers=headers, content=content)
            finally:
                self._last[host] = time.monotonic()
        text = r.text
        if len(text) > config.MAX_BODY_BYTES:
            text = text[: config.MAX_BODY_BYTES]
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        cookies = r.headers.get_list("set-cookie")
        if cookies:
            hdrs["set-cookie"] = "\n".join(cookies)
        log.debug("%s %s -> %s", method, url, r.status_code)
        return Response(url=str(r.url), status=r.status_code, headers=hdrs, text=text)

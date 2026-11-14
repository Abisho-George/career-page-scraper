"""Offline HTTP for tests: route URLs to fixture files with httpx.MockTransport."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from scraper.fetch import Fetcher

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture(name))


class Router:
    """routes: {"https://x/y": spec, "POST https://x/z": spec}; spec = (status, body[, headers]) or
    a callable(request) -> spec. Unknown URLs -> 404. Every request is recorded in .calls."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        spec = self.routes.get(f"{request.method} {url}", self.routes.get(url))
        if spec is None and request.url.path == "/robots.txt":
            spec = (404, "")
        if spec is None:
            spec = (404, "<html><head><title>Not Found</title></head><body>nope</body></html>")
        if callable(spec):
            spec = spec(request)
        if isinstance(spec, Exception):
            raise spec
        status, body, *rest = spec
        headers = rest[0] if rest else {}
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers = {"content-type": "application/json", **headers}
        elif "content-type" not in {k.lower() for k in headers}:
            headers = {"content-type": "text/html; charset=utf-8", **headers}
        return httpx.Response(status, text=body, headers=headers, request=request)

    def fetcher(self, **kw) -> Fetcher:
        kw.setdefault("min_delay", 0)
        kw.setdefault("max_delay", 0)
        return Fetcher(transport=httpx.MockTransport(self), **kw)

    def urls(self) -> list[str]:
        return [u for _, u in self.calls]

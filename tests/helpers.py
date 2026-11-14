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

    def __init__(self, routes: dict | None = None, default_content_type: bool = True):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str]] = []
        self.default_content_type = default_content_type  # False: serve saved headers as they were

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
        elif self.default_content_type and "content-type" not in {k.lower() for k in headers}:
            headers = {"content-type": "text/html; charset=utf-8", **headers}
        return httpx.Response(status, text=body, headers=headers, request=request)

    def fetcher(self, **kw) -> Fetcher:
        kw.setdefault("min_delay", 0)
        kw.setdefault("max_delay", 0)
        return Fetcher(transport=httpx.MockTransport(self), **kw)

    def urls(self) -> list[str]:
        return [u for _, u in self.calls]


# ---------------------------------------------------------------- replaying a saved live run

_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}


def _replay_headers(headers: dict) -> dict:
    out = {k: v for k, v in (headers or {}).items() if k.lower() not in _DROP_HEADERS}
    for k in list(out):
        if k.lower() == "content-type":  # the saved file is UTF-8 whatever the site sent
            out[k] = out[k].split(";")[0].strip() + "; charset=utf-8"
    return out


def load_recording(folder) -> tuple[Router, dict[str, dict], dict]:
    """Turn a folder written by `main.py company <domain> --save-fixtures` back into
    (Router serving exactly those responses, {requested url: rendered page}, summary dict).

    Each (method, url, request body) gets its saved responses in order, the last one repeating,
    so a request that failed once and succeeded on retry replays the same way. Saved network
    errors are raised again; anything not saved is a 404. Headers are served as saved (no
    default content-type is added). A rendered page is {"text", "status", "headers", "final_url"}."""
    folder = Path(folder)
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    summary_path = folder / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    queues: dict[tuple[str, str, str | None], list[dict]] = {}
    urls: dict[str, set[str]] = {}
    renders: dict[str, dict] = {}
    for e in index["entries"]:
        if e["kind"] == "rendered":
            renders[e["url"]] = {"text": (folder / e["file"]).read_text(encoding="utf-8"), "status": e["status"],
                                 "headers": e.get("headers") or {}, "final_url": e.get("final_url") or e["url"]}
            continue
        url = str(httpx.URL(e["url"]))
        body = e.get("request_body") if e["method"] != "GET" else None
        queues.setdefault((e["method"], url, body), []).append(e)
        urls.setdefault(f"{e['method']} {url}", set()).add(url)

    def spec_for(e):
        if e["kind"] == "error":
            exc = getattr(httpx, e.get("error") or "", None)
            if not (isinstance(exc, type) and issubclass(exc, httpx.HTTPError)):
                exc = httpx.ConnectError
            return exc(f"replayed {e.get('error')}")
        return (e["status"], (folder / e["file"]).read_text(encoding="utf-8"), _replay_headers(e["headers"]))

    def respond(request):
        body = None
        if request.method != "GET":
            body = request.content.decode("utf-8", "replace") if request.content else None
        queue = queues.get((request.method, str(request.url), body))
        if not queue:
            return (404, "")
        return spec_for(queue.pop(0) if len(queue) > 1 else queue[0])

    return Router({key: respond for key in urls}, default_content_type=False), renders, summary

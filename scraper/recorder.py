"""Save every raw response a debug run fetches, so a live run can become an offline test.

Used by `python main.py company <domain> --save-fixtures`. Writes, under
tests/fixtures/live/<domain>/:

    001_GET_acme.com_robots.txt.txt
    002_GET_acme.com_careers.html
    003_GET_boards-api.greenhouse.io_v1_boards_acme_jobs_content-true.json
    ...
    index.json     one entry per file: method, url, status, headers, request body, kind
    summary.json   ats, token, method, job count, status, reason (what a replay should reproduce)

tests/helpers.py: load_recording(dir) turns a folder back into a mock Router for replay tests.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import config

LIVE_DIR = config.ROOT / "tests" / "fixtures" / "live"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(url: str, max_len: int = 90) -> str:
    p = urlsplit(url)
    parts = [p.hostname or "", p.path.strip("/").replace("/", "_")]
    if p.query:
        parts.append(p.query.replace("=", "-").replace("&", "_"))
    s = _SAFE.sub("-", "_".join(x for x in parts if x)).strip("-_")
    return s[:max_len] or "root"


def _pretty(text: str) -> tuple[str, str]:
    """(file extension, file text). JSON is pretty-printed for reading; the values are unchanged."""
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            return "json", json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if "<html" in text[:2000].lower() or "<!doctype" in text[:200].lower():
        return "html", text
    return "txt", text


class FixtureRecorder:
    def __init__(self, out_dir: Path):
        self.dir = Path(out_dir)
        if self.dir.exists():
            shutil.rmtree(self.dir)  # a re-run replaces the previous recording of this domain
        self.dir.mkdir(parents=True)
        self.entries: list[dict] = []

    def record(self, method: str, url: str, status: int, headers: dict, text: str,
               request_body: str | None = None, kind: str = "http", error: str | None = None) -> Path:
        """kind: "http" (a response), "rendered" (browser HTML) or "error" (no response: `error`
        holds the exception name, e.g. ConnectError, and a replay raises it again)."""
        n = len(self.entries) + 1
        ext, body = _pretty(text or "")
        label = {"rendered": "RENDERED", "error": f"{method.upper()}-ERROR"}.get(kind, method.upper())
        name = f"{n:03d}_{label}_{_slug(url)}.{ext}"
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        self.entries.append({
            "file": name, "kind": kind, "method": method.upper(), "url": url, "status": status,
            "headers": dict(headers or {}), "request_body": request_body, "error": error,
        })
        self._write_index()
        return path

    def _write_index(self) -> None:
        (self.dir / "index.json").write_text(
            json.dumps({"entries": self.entries}, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_summary(self, summary: dict) -> Path:
        summary = {**summary, "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                   "files": len(self.entries)}
        path = self.dir / "summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

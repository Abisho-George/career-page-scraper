"""Real headless-Chromium test against a local HTTP server (no internet). Skipped if Chromium
isn't installed (run `playwright install chromium`)."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scraper.browser import Browser, looks_js_rendered
from tests.helpers import fixture

SPA = """<!DOCTYPE html><html><head><title>Careers</title></head><body><div id="root"></div>
<script>
const jobs = ["Backend Engineer", "Sales Lead", "Designer", "Support Agent"];
let shown = 2;
function draw() {
  document.getElementById('root').innerHTML =
    '<h1>Open roles</h1><ul>' + jobs.slice(0, shown).map((t, i) =>
      `<li><a href="/jobs/${t.toLowerCase().replace(' ', '-')}-${i}">${t}</a></li>`).join('') + '</ul>' +
    (shown < jobs.length ? '<button id="more">Load more</button>' : '');
  const b = document.getElementById('more');
  if (b) b.onclick = () => { shown += 1; setTimeout(draw, 50); };
}
setTimeout(draw, 100);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = SPA.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_looks_js_rendered():
    assert looks_js_rendered(fixture("spa_shell.html"))
    assert not looks_js_rendered(fixture("normal_careers.html"))


def test_render_clicks_load_more(server):
    async def go():
        b = Browser()
        try:
            return await b.render(server + "/careers")
        finally:
            await b.close()
    try:
        resp = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            pytest.skip("chromium not installed")
        raise
    assert resp.status == 200
    for title in ("Backend Engineer", "Sales Lead", "Designer", "Support Agent"):
        assert title in resp.text
    from selectolax.lexbor import LexborHTMLParser
    assert LexborHTMLParser(resp.text).css_first("button#more") is None  # gone once all loaded

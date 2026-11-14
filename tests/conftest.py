import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def conn():
    from scraper import db
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Belt and braces: any Fetcher built without a mock transport fails loudly."""
    import httpx
    real_init = httpx.AsyncClient.__init__

    def guarded(self, *a, **kw):
        if kw.get("transport") is None:
            raise RuntimeError("live network is not allowed in tests; use tests.helpers.Router")
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", guarded)

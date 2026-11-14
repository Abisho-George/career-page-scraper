from scraper import sheets


def test_normalize_domain():
    assert sheets.normalize_domain("https://www.Acme.com/about?x=1") == "acme.com"
    assert sheets.normalize_domain("acme.co.in") == "acme.co.in"
    assert sheets.normalize_domain("  WWW.example.org/ ") == "example.org"
    assert sheets.normalize_domain("") == ""


def test_rows_to_companies_dedupes_and_fixes_urls():
    rows = [
        {"company_name": "Acme", "website": "https://acme.com", "careers_url": ""},
        {"company_name": "Acme again", "website": "www.acme.com", "careers_url": ""},
        {"Company Name": "Globex", "Website": "globex.com", "Careers URL": "globex.com/jobs"},
        {"company_name": "", "website": "", "careers_url": ""},
    ]
    out = sheets._rows_to_companies(rows)
    assert out == [
        {"company_name": "Acme", "website": "acme.com", "careers_url": ""},
        {"company_name": "Globex", "website": "globex.com", "careers_url": "https://globex.com/jobs"},
    ]


def test_csv(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("company_name,website,careers_url\nAcme,acme.com,\n", encoding="utf-8")
    assert sheets.read_companies_csv(p) == [{"company_name": "Acme", "website": "acme.com", "careers_url": ""}]


class FakeWS:
    def __init__(self):
        self.values, self.cleared, self.size = [], False, None

    def clear(self):
        self.cleared, self.values = True, []

    def resize(self, rows, cols):
        self.size = (rows, cols)

    def update(self, values, range_name, value_input_option):
        assert value_input_option == "RAW"
        start = int(range_name[1:]) - 1
        self.values[start:start + len(values)] = values

    def get_all_records(self):
        return [{"company_name": "Acme", "website": "https://www.acme.com/", "careers_url": ""}]


class FakeSpreadsheet:
    def __init__(self):
        self.tabs = {"Companies": FakeWS()}

    def worksheet(self, name):
        import gspread
        if name not in self.tabs:
            raise gspread.exceptions.WorksheetNotFound(name)
        return self.tabs[name]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS()
        return self.tabs[title]


def test_read_and_write_tabs(monkeypatch):
    monkeypatch.setattr(sheets, "CHUNK_ROWS", 2)
    ss = FakeSpreadsheet()
    assert sheets.read_companies(ss) == [{"company_name": "Acme", "website": "acme.com", "careers_url": ""}]
    rows = [["h1", "h2"], ["a", "=SUM(1)"], ["b", 2], ["c", "x" * 60000]]
    sheets.write_tab("Jobs", rows, ss)
    ws = ss.tabs["Jobs"]
    assert ws.cleared and ws.size == (5, 2)
    assert ws.values[1] == ["a", "=SUM(1)"] and ws.values[2] == ["b", 2]
    assert len(ws.values[3][1]) == sheets.MAX_CELL

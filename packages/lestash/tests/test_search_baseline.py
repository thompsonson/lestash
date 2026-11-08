"""scripts/search_baseline.py, against a stand-in server (#215)."""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "search_baseline.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("search_baseline", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = _load_script()


class _Search(BaseHTTPRequestHandler):
    seen: list[dict[str, list[str]]] = []

    def do_GET(self) -> None:
        url = urlparse(self.path)
        params = parse_qs(url.query)
        _Search.seen.append(params)
        if url.path != "/api/items/search":
            self.send_error(404)
            return
        if params["mode"] == ["semantic"]:
            self.send_error(500, "Vector search unavailable")
            return
        items = [
            {"id": 7, "source_type": "arxiv", "source_id": "2512.20660", "title": "Dual-State"},
            {"id": 9, "source_type": "youtube", "source_id": "liked:x", "title": None},
        ]
        body = json.dumps({"items": items, "total": 2, "limit": 10, "offset": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Search.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Search)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()


def test_the_committed_queries_parse() -> None:
    queries = baseline.load_queries(baseline.DEFAULT_QUERIES)
    assert len(queries) >= 20
    kinds = {"title", "topic", "paraphrase", "deep"}
    assert all(q["expect"] and q["kind"] in kinds for q in queries)


def test_it_records_each_mode_and_where_expected_items_rank(server: str, tmp_path: Path) -> None:
    queries = tmp_path / "queries.toml"
    queries.write_text(
        '[[query]]\nq = "dual state"\nkind = "title"\n'
        'expect = ["arxiv:2512.20660", "youtube:liked:missing"]\n'
    )
    out = tmp_path / "results.json"

    code = baseline.main(["--url", server, "--queries", str(queries), "--out", str(out)])

    assert code == 1  # the semantic mode failed, and that is reported
    report = json.loads(out.read_text())
    entry = report["results"][0]
    assert entry["keyword"][0] == {
        "rank": 1,
        "id": 7,
        "key": "arxiv:2512.20660",
        "title": "Dual-State",
    }
    assert entry["keyword"][1]["key"] == "youtube:liked:x"
    assert "error" in entry["semantic"]
    assert baseline.expected_ranks(entry, "hybrid") == "1, -"
    assert baseline.expected_ranks(entry, "semantic") == "error"
    assert {p["mode"][0] for p in _Search.seen} == {"keyword", "semantic", "hybrid"}
    assert all(p["limit"] == ["10"] for p in _Search.seen)

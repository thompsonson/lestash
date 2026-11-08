"""Search with punctuation, the API's 404 for unknown paths, and the reported version."""

from importlib.metadata import version

import lestash_server.deps as deps
import pytest
from fastapi.testclient import TestClient
from lestash.core.database import get_connection
from lestash_server.app import create_app

PUNCTUATED = [
    "Dual-State Architecture",
    "v2.0",
    "C++",
    "don't",
    "title:x",
    "a/b",
    "(Python)",
    '"unbalanced',
    "Gödel",
    "++",
    "AND",
    "Python NOT",
]


@pytest.fixture
def seeded(test_config):
    with get_connection(test_config) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content) "
            "VALUES ('note', 'n-dual', 'The Dual-State Architecture', 'Release v2.0 of Gödel')"
        )
        conn.commit()
    return test_config


@pytest.mark.parametrize("q", PUNCTUATED)
def test_keyword_search_never_rejects_punctuation(client, seeded, q):
    resp = client.get("/api/items/search", params={"q": q, "mode": "keyword"})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["items"], list)


@pytest.mark.parametrize(
    "q",
    ["Dual-State Architecture", "dual-state", "v2.0", "Gödel", "godel", '"Dual State'],
)
def test_keyword_search_finds_punctuated_words(client, seeded, q):
    resp = client.get("/api/items/search", params={"q": q, "mode": "keyword"})
    titles = [i["title"] for i in resp.json()["items"]]
    assert "The Dual-State Architecture" in titles


def test_hybrid_keeps_keyword_hits_for_punctuated_words(client, seeded):
    # Semantic search may be unavailable in tests; the keyword half must still find it
    resp = client.get("/api/items/search", params={"q": "Dual-State", "mode": "hybrid"})
    assert resp.status_code == 200
    assert "The Dual-State Architecture" in [i["title"] for i in resp.json()["items"]]


def test_nothing_searchable_returns_no_results(client):
    resp = client.get("/api/items/search", params={"q": "++", "mode": "keyword"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.fixture
def web_client(test_config, tmp_path):
    """An app serving a static frontend, so the SPA fallback is active."""
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "app.js").write_text("console.log(1)")
    original = deps._config
    deps._config = test_config
    with TestClient(create_app(static_dir=str(tmp_path))) as tc:
        yield tc
    deps._config = original


@pytest.mark.parametrize("path", ["/api/does-not-exist", "/api/items/1/nope", "/api", "/api/"])
def test_unknown_api_path_is_a_json_404(web_client, path):
    resp = web_client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/", "/notes", "/item/42", "/apiary"])
def test_other_paths_still_get_the_app(web_client, path):
    resp = web_client.get(path)
    assert resp.status_code == 200
    assert resp.text == "<html>app</html>"


def test_static_files_are_served(web_client):
    assert web_client.get("/app.js").text == "console.log(1)"


def test_known_api_paths_still_work_with_a_frontend(web_client):
    assert web_client.get("/api/health").json()["status"] == "ok"


def test_health_reports_the_installed_release(client):
    assert client.get("/api/health").json()["version"] == version("lestash-server")


def test_openapi_reports_the_installed_release(client):
    assert client.get("/openapi.json").json()["info"]["version"] == version("lestash-server")


def test_cli_reports_the_installed_release():
    from lestash import __version__

    assert __version__ == version("lestash")

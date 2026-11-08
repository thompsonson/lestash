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


def test_semantic_hits_respect_include_children(client, seeded, monkeypatch):
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        parent = conn.execute("SELECT id FROM items WHERE source_id = 'n-dual'").fetchone()[0]
        child = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, parent_id) "
            "VALUES ('note', 'n-child', 'A child', 'x', ?)",
            (parent,),
        ).lastrowid
        conn.commit()
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(child, 0.1), (parent, 0.2)])

    def ids(include_children):
        resp = client.get(
            "/api/items/search",
            params={"q": "zzz", "mode": "semantic", "include_children": include_children},
        )
        return [i["id"] for i in resp.json()["items"]]

    assert ids(True) == [child, parent]
    assert ids(False) == [parent]


def test_nothing_searchable_returns_no_results(client):
    resp = client.get("/api/items/search", params={"q": "++", "mode": "keyword"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_hybrid_keeps_semantic_results_when_the_keyword_index_fails(client, monkeypatch):
    """A broken items_fts must not take hybrid search down with it."""
    import lestash.core.embeddings as emb

    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(1, 0.1)])
    with deps.get_db() as conn:
        conn.execute("DROP TABLE items_fts")
        conn.commit()

    resp = client.get("/api/items/search", params={"q": "Python", "mode": "hybrid"})
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["items"]] == [1]
    resp = client.get("/api/items/search", params={"q": "Python", "mode": "keyword"})
    assert resp.status_code == 500


@pytest.fixture
def web_client(test_config, tmp_path, monkeypatch):
    """An app serving a static frontend, so the SPA fallback is active.

    The frontend sits in tmp_path/web, beside a file the fallback must never serve.
    """
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>app</html>")
    (web / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    monkeypatch.setattr(deps, "_config", test_config)
    with TestClient(create_app(static_dir=str(web))) as tc:
        yield tc


@pytest.mark.parametrize("path", ["/api/does-not-exist", "/api/items/1/nope", "/api", "/api/"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_unknown_api_path_is_a_json_404(web_client, path, method):
    resp = web_client.request(method, path)
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


@pytest.mark.parametrize(
    "path",
    ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/..%2F..%2Fsecret.txt", "/a/..%2f..%2fsecret.txt"],
)
def test_fallback_never_serves_files_outside_the_frontend(web_client, path):
    resp = web_client.get(path)
    assert "TOP SECRET" not in resp.text
    assert resp.text == "<html>app</html>"


def test_other_methods_on_app_paths_are_not_found(web_client):
    assert web_client.post("/notes").status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [("DELETE", "/api/health"), ("POST", "/api/items/search"), ("PUT", "/api/health")],
)
def test_known_api_path_with_the_wrong_method_is_405(web_client, client, method, path):
    """The same answer with or without the web app served."""
    assert web_client.request(method, path).status_code == 405
    assert client.request(method, path).status_code == 405


@pytest.mark.parametrize("path", ["//api/foo", "//api//nope"])
def test_api_paths_with_extra_slashes_are_still_404(web_client, path):
    # A full URL: httpx would read a bare "//api/foo" as host "api"
    resp = web_client.get(f"http://testserver{path}")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_head_on_an_app_path_gets_the_app(web_client):
    assert web_client.head("/notes").status_code == 200


def test_known_api_paths_still_work_with_a_frontend(web_client):
    assert web_client.get("/api/health").json()["status"] == "ok"


def test_health_reports_the_installed_release(client):
    assert client.get("/api/health").json()["version"] == version("lestash-server")


def test_openapi_reports_the_installed_release(client):
    assert client.get("/openapi.json").json()["info"]["version"] == version("lestash-server")


def test_cli_reports_the_installed_release():
    from lestash import __version__

    assert __version__ == version("lestash")

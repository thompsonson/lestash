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
            params={
                "q": "zzz",
                "mode": "semantic",
                "include_children": include_children,
                "group": False,
            },
        )
        return [i["id"] for i in resp.json()["items"]]

    # Ungrouped: parts are results of their own, or dropped
    assert ids(True) == [child, parent]
    assert ids(False) == [parent]


@pytest.mark.parametrize("mode", ["Keyword", "fts", ""])
def test_an_unknown_search_mode_is_rejected(client, mode):
    """Not an empty 200, which would read as "no results"."""
    resp = client.get("/api/items/search", params={"q": "Python", "mode": mode})
    assert resp.status_code == 422


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


def test_hybrid_reports_when_both_halves_fail(client, monkeypatch):
    """An empty 200 would read as "no results"."""
    import lestash.core.embeddings as emb

    def broken(q):
        raise RuntimeError("no model")

    monkeypatch.setattr(emb, "embed_text", broken)
    with deps.get_db() as conn:
        conn.execute("DROP TABLE items_fts")
        conn.commit()
    resp = client.get("/api/items/search", params={"q": "Python", "mode": "hybrid"})
    assert resp.status_code == 500


def test_hybrid_reports_a_keyword_failure_with_no_semantic_hits(client, monkeypatch):
    """The semantic half succeeds with nothing; the 500 must come from the keyword check."""
    import lestash.core.embeddings as emb

    ran = []
    monkeypatch.setattr(emb, "load_vec_extension", lambda conn: None)
    monkeypatch.setattr(emb, "ensure_vec_table", lambda conn: None)
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: ran.append(1) or [])
    with deps.get_db() as conn:
        conn.execute("DROP TABLE items_fts")
        conn.commit()
    resp = client.get("/api/items/search", params={"q": "Python", "mode": "hybrid"})
    assert resp.status_code == 500
    assert ran == [1]  # the vector search ran and returned nothing


def test_hybrid_still_answers_when_only_semantic_fails(client, monkeypatch):
    import lestash.core.embeddings as emb

    def broken(q):
        raise RuntimeError("no model")

    monkeypatch.setattr(emb, "embed_text", broken)
    resp = client.get("/api/items/search", params={"q": "Python", "mode": "hybrid"})
    assert resp.status_code == 200
    assert resp.json()["items"]


def test_semantic_without_children_asks_for_more_hits(client, monkeypatch):
    """Children are filtered after the vector search, so it must fetch extra."""
    import lestash.core.embeddings as emb

    asked = []
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: asked.append(limit) or [])
    client.get(
        "/api/items/search",
        params={"q": "x", "mode": "semantic", "limit": 5, "group": False},
    )
    client.get(
        "/api/items/search",
        params={"q": "x", "mode": "semantic", "limit": 5, "include_children": False},
    )
    # Grouped, the default: parts of one item take one place
    client.get("/api/items/search", params={"q": "x", "mode": "semantic", "limit": 5})
    assert asked == [10, 40, 40]


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


def test_app_files_are_checked_on_every_load(web_client):
    """After a deploy the browser must not run old modules beside new ones."""
    assert web_client.get("/app.js").headers["cache-control"] == "no-cache"
    assert web_client.get("/items/5").headers["cache-control"] == "no-cache"


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


def test_an_encoded_question_mark_is_part_of_the_path(web_client, tmp_path):
    (tmp_path / "web" / "a?b.txt").write_text("odd name")
    assert web_client.get("/a%3Fb.txt").text == "odd name"


def test_unknown_api_paths_404_under_a_root_path(test_config, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>app</html>")
    monkeypatch.setattr(deps, "_config", test_config)
    app = create_app(static_dir=str(tmp_path))
    (tmp_path / "ed.js").write_text("ED")
    with TestClient(app, root_path="/stash") as tc:
        resp = tc.get("/stash/api/nope")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        # Only a whole "/stash" segment is the root path
        assert tc.get("/stashed.js").text == "<html>app</html>"
        assert tc.get("/stashapi/nope").text == "<html>app</html>"


def test_a_root_path_with_a_trailing_slash(test_config, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>app</html>")
    monkeypatch.setattr(deps, "_config", test_config)
    with TestClient(create_app(static_dir=str(tmp_path)), root_path="/stash/") as tc:
        resp = tc.get("/stash/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_a_missing_static_asset_is_a_404(web_client):
    resp = web_client.get("/static/missing.js")
    assert resp.status_code == 404
    assert "app" not in resp.text


@pytest.mark.parametrize("path", ["/%00", "/a%00b", "/" + "a" * 300])
def test_unusual_paths_get_the_app_not_a_500(web_client, path):
    resp = web_client.get(path)
    assert resp.status_code == 200
    assert resp.text == "<html>app</html>"


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


def test_hybrid_ranks_a_near_empty_item_below_a_full_one(client, monkeypatch):
    """A session named after the query, with no text, used to top hybrid results (#267)."""
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        thin = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content)"
            " VALUES ('claude-code', 'thin', 'zebracorn', 'zebracorn (2026-03-13)')"
        ).lastrowid
        full = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content)"
            " VALUES ('notebooklm', 'full', 'A notebook', ?)",
            ("A long notebook about many things. " * 20,),
        ).lastrowid
        conn.commit()
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    # The thin item is the nearest vector, the full one second
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(thin, 0.1), (full, 0.2)])
    resp = client.get("/api/items/search", params={"q": "qqqnothing", "mode": "hybrid"})
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["items"]][:2] == [full, thin]


def test_hybrid_keeps_the_weight_of_an_item_with_parts(client, monkeypatch):
    """A chat's text is in its turns: a short parent is not a near-empty item."""
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        chat = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content)"
            " VALUES ('claude', 'chat', 'zebracorn', 'zebracorn chat')"
        ).lastrowid
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, parent_id)"
            " VALUES ('claude', 'turn', 'a long turn', ?)",
            (chat,),
        )
        full = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content)"
            " VALUES ('notebooklm', 'full2', 'A notebook', ?)",
            ("A long notebook about many things. " * 20,),
        ).lastrowid
        conn.commit()
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(chat, 0.1), (full, 0.2)])
    resp = client.get("/api/items/search", params={"q": "qqqnothing", "mode": "hybrid"})
    assert [i["id"] for i in resp.json()["items"]][:2] == [chat, full]


def test_a_part_hit_without_children_brings_its_item(client, seeded, monkeypatch):
    """A round or comment that matches counts for its item, at its best part's rank (#221)."""
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        parent = conn.execute("SELECT id FROM items WHERE source_id = 'n-dual'").fetchone()[0]
        other = conn.execute("SELECT id FROM items WHERE id != ? LIMIT 1", (parent,)).fetchone()[0]
        child = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, parent_id) "
            "VALUES ('note', 'n-part', 'A part', 'x', ?)",
            (parent,),
        ).lastrowid
        conn.commit()
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(child, 0.1), (other, 0.2)])

    resp = client.get(
        "/api/items/search",
        params={"q": "zzz", "mode": "semantic", "include_children": False},
    )

    assert [i["id"] for i in resp.json()["items"]] == [parent, other]


def test_similar_items_leave_out_the_items_own_parts(client, seeded, monkeypatch):
    """Related items compare whole items: not its rounds, comments or siblings (#221)."""
    from lestash_server.routes import embeddings as route

    with deps.get_db() as conn:
        item = conn.execute("SELECT id FROM items WHERE source_id = 'n-dual'").fetchone()[0]
        other = conn.execute("SELECT id FROM items WHERE id != ? LIMIT 1", (item,)).fetchone()[0]
        part = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, parent_id) "
            "VALUES ('note', 'n-own-part', 'Its part', 'x', ?)",
            (item,),
        ).lastrowid
        conn.commit()
    monkeypatch.setattr(route, "load_vec_extension", lambda conn: None)
    monkeypatch.setattr(route, "ensure_vec_table", lambda conn: None)
    monkeypatch.setattr(route, "stored_vector", lambda conn, item_id: None)
    monkeypatch.setattr(route, "embed_query", lambda conn, text: [0.0] * 384)
    monkeypatch.setattr(
        route, "search_similar", lambda conn, e, limit: [(item, 0), (part, 0.1), (other, 0.2)]
    )

    resp = client.get(f"/api/embeddings/similar/{item}")
    assert [i["id"] for i in resp.json()] == [other]
    resp = client.get(f"/api/embeddings/similar/{part}")
    assert [i["id"] for i in resp.json()] == [other]


def test_similar_items_search_with_the_items_own_vector(client, seeded, monkeypatch):
    """Its stored vector, made with the document prompt like the rest; no query
    embedding (#307). An item without one is embedded as before."""
    from lestash_server.routes import embeddings as route

    with deps.get_db() as conn:
        item, other = (r[0] for r in conn.execute("SELECT id FROM items LIMIT 2"))
    stored = {item: [0.25] * 768}
    searched, embedded = [], []

    def search(conn, e, limit):
        searched.append(e)
        return [(other, 0.1)]

    monkeypatch.setattr(route, "load_vec_extension", lambda conn: None)
    monkeypatch.setattr(route, "ensure_vec_table", lambda conn: None)
    monkeypatch.setattr(route, "stored_vector", lambda conn, item_id: stored.get(item_id))
    monkeypatch.setattr(route, "embed_query", lambda conn, text: embedded.append(text) or [0.5])
    monkeypatch.setattr(route, "search_similar", search)

    assert [i["id"] for i in client.get(f"/api/embeddings/similar/{item}").json()] == [other]
    assert searched == [[0.25] * 768] and embedded == []

    client.get(f"/api/embeddings/similar/{other}")
    assert searched[-1] == [0.5] and len(embedded) == 1


def _chat_with_turns(conn):
    chat = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content) "
        "VALUES ('claude', 'c-grp', 'Planning chat', 'Hello') RETURNING id"
    ).fetchone()[0]
    turns = [
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, parent_id) "
            "VALUES ('claude', ?, NULL, ?, ?) RETURNING id",
            (f"t-grp-{n}", text, chat),
        ).fetchone()[0]
        for n, text in enumerate(
            ["How do PDDL templates work?", "PDDL templates fill problem files from data."]
        )
    ]
    conn.commit()
    return chat, turns


def test_keyword_hits_on_turns_give_one_result_for_their_chat(client, seeded):
    """Grouped (the default): one card per item, at its best part (#221)."""
    with deps.get_db() as conn:
        chat, turns = _chat_with_turns(conn)

    resp = client.get("/api/items/search", params={"q": "PDDL", "mode": "keyword"})
    items = resp.json()["items"]

    assert [i["id"] for i in items] == [chat]
    assert items[0]["match_id"] in turns
    assert items[0]["match_label"] == "Turn"
    assert "<<PDDL>>" in items[0]["preview"]


def test_ungrouped_keyword_search_lists_the_turns(client, seeded):
    with deps.get_db() as conn:
        chat, turns = _chat_with_turns(conn)

    resp = client.get("/api/items/search", params={"q": "PDDL", "mode": "keyword", "group": False})

    assert sorted(i["id"] for i in resp.json()["items"]) == sorted(turns)


def test_a_vector_hit_on_a_round_shows_its_text(client, seeded, monkeypatch):
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        chat, turns = _chat_with_turns(conn)
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(
        emb, "search_similar", lambda conn, e, limit: [(turns[0], 0.1), (turns[1], 0.2)]
    )

    resp = client.get("/api/items/search", params={"q": "zzz", "mode": "semantic"})
    items = resp.json()["items"]

    assert [i["id"] for i in items] == [chat]
    assert items[0]["match_id"] == turns[0]
    assert items[0]["preview"] == "How do PDDL templates work?"


def test_an_item_found_itself_and_by_a_part_takes_its_best_rank(client, seeded, monkeypatch):
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        chat, turns = _chat_with_turns(conn)
        other = conn.execute("SELECT id FROM items WHERE source_id = 'n-dual'").fetchone()[0]
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(
        emb,
        "search_similar",
        lambda conn, e, limit: [(turns[1], 0.1), (other, 0.2), (chat, 0.3)],
    )

    resp = client.get("/api/items/search", params={"q": "zzz", "mode": "semantic"})

    assert [i["id"] for i in resp.json()["items"]] == [chat, other]


def test_a_keyword_hit_on_the_item_keeps_it_from_a_vector_part(client, seeded, monkeypatch):
    """Review round 1 (r1.1): the card opens where its snippet came from."""
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        chat, turns = _chat_with_turns(conn)
        conn.execute("UPDATE items SET content = 'PDDL overview' WHERE id = ?", (chat,))
        conn.execute("UPDATE items SET content = 'unrelated' WHERE id IN (?, ?)", turns)
        conn.commit()
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(turns[1], 0.1)])

    resp = client.get("/api/items/search", params={"q": "PDDL", "mode": "hybrid"})
    found = next(i for i in resp.json()["items"] if i["id"] == chat)

    assert found["match_id"] is None
    assert "<<PDDL>>" in found["preview"]


def _kinds_seed(conn):
    """A chat whose turn says PDDL, a paper and a note that say it too."""
    chat = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content) "
        "VALUES ('claude', 'c-kinds', 'A chat', 'Hello') RETURNING id"
    ).fetchone()[0]
    turn = conn.execute(
        "INSERT INTO items (source_type, source_id, content, parent_id) "
        "VALUES ('claude', 't-kinds', 'PDDL problem files', ?) RETURNING id",
        (chat,),
    ).fetchone()[0]
    paper = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content) "
        "VALUES ('arxiv', 'a-kinds', 'PDDL planning', 'Planning') RETURNING id"
    ).fetchone()[0]
    note = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content) "
        "VALUES ('note', 'n-kinds', 'PDDL notes', 'Mine') RETURNING id"
    ).fetchone()[0]
    conn.commit()
    return chat, turn, paper, note


def test_search_counts_the_kinds_it_found(client, seeded):
    """Facets for the kind chips (#268): grouped, a turn counts as its chat."""
    with deps.get_db() as conn:
        _kinds_seed(conn)

    resp = client.get("/api/items/search", params={"q": "PDDL", "mode": "keyword"})
    facets = {f["kind"]: (f["label"], f["count"]) for f in resp.json()["facets"]}

    assert facets == {"chat": ("Chats", 1), "paper": ("Papers", 1), "note": ("Notes", 1)}


def test_a_kind_filter_keeps_the_facets(client, seeded):
    with deps.get_db() as conn:
        chat, _turn, _paper, _note = _kinds_seed(conn)

    resp = client.get("/api/items/search", params={"q": "PDDL", "mode": "keyword", "kind": "chat"})
    body = resp.json()

    assert [i["id"] for i in body["items"]] == [chat]
    assert {f["kind"] for f in body["facets"]} == {"chat", "paper", "note"}


def test_several_kinds_can_be_asked_for(client, seeded):
    with deps.get_db() as conn:
        _chat, _turn, paper, note = _kinds_seed(conn)

    resp = client.get(
        "/api/items/search",
        params=[("q", "PDDL"), ("mode", "keyword"), ("kind", "paper"), ("kind", "note")],
    )

    assert sorted(i["id"] for i in resp.json()["items"]) == sorted([paper, note])


def test_a_kind_filter_applies_to_vector_hits_by_their_item(client, seeded, monkeypatch):
    import lestash.core.embeddings as emb

    with deps.get_db() as conn:
        chat, turn, paper, _note = _kinds_seed(conn)
    monkeypatch.setattr(emb, "embed_text", lambda q: [0.0] * 384)
    monkeypatch.setattr(emb, "search_similar", lambda conn, e, limit: [(turn, 0.1), (paper, 0.2)])

    def ids(kind):
        params = {"q": "zzz", "mode": "semantic", "kind": kind}
        return [i["id"] for i in client.get("/api/items/search", params=params).json()["items"]]

    assert ids("chat") == [chat]
    assert ids("paper") == [paper]


def test_a_chips_count_is_what_it_returns(client, seeded):
    """Review round 1 (r1.1): counts come from the results, not the candidate lists."""
    with deps.get_db() as conn:
        _kinds_seed(conn)
        for n in range(3):
            conn.execute(
                "INSERT INTO items (source_type, source_id, title, content) "
                "VALUES ('note', ?, 'PDDL more', 'x')",
                (f"n-more-{n}",),
            )
        conn.commit()

    body = client.get("/api/items/search", params={"q": "PDDL", "mode": "keyword"}).json()
    for facet in body["facets"]:
        chosen = client.get(
            "/api/items/search", params={"q": "PDDL", "mode": "keyword", "kind": facet["kind"]}
        ).json()
        assert len(chosen["items"]) == facet["count"]
        assert {i["kind"] for i in chosen["items"]} == {facet["kind"]}

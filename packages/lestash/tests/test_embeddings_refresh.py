"""Vectors kept up to date with their items (#221): one recipe, a record of each vector,
and a refresh that embeds what is new or changed and drops what should have none."""

import hashlib

import lestash.core.embeddings as emb
import pytest
from lestash.core.database import get_connection

CALLS: list[list[str]] = []


def _fake_vector(text: str) -> list[float]:
    """A unit vector that depends on the text, so a change of text changes the vector."""
    seed = hashlib.sha256(text.encode()).digest()
    vector = [b / 255 for b in (seed * 12)[: emb.EMBEDDING_DIM]]
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector]


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    CALLS.clear()

    def batch(texts):
        CALLS.append(list(texts))
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(emb, "embed_batch", batch)
    monkeypatch.setattr(emb, "embed_text", _fake_vector)


@pytest.fixture
def conn(test_db):
    with get_connection(test_db) as conn:
        emb.load_vec_extension(conn)
        yield conn


def _add(conn, source_id, title, content, *, parent_id=None, held=1):
    item_id = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content, parent_id, held) "
        "VALUES ('note', ?, ?, ?, ?, ?)",
        (source_id, title, content, parent_id, held),
    ).lastrowid
    conn.commit()
    return item_id


def _vector_ids(conn):
    return {row[0] for row in conn.execute("SELECT item_id FROM vec_items")}


def test_the_recipe_is_the_title_then_the_start_of_the_content():
    assert emb.embed_source("Title", "body") == "Title body"
    assert emb.embed_source(None, "body") == "body"
    assert emb.embed_source("Title", None) == "Title"
    assert emb.embed_source(None, None) == ""
    assert emb.embed_source("T", "x" * 900) == "T " + "x" * emb.CONTENT_CHARS


def test_refresh_embeds_held_top_level_items_and_records_each(conn):
    post = _add(conn, "a", "A post", "Its text")
    child = _add(conn, "b", "A reply", "text", parent_id=post)
    stub = _add(conn, "c", "A stub", "known by its address", held=0)
    empty = _add(conn, "d", None, "")

    counts = emb.refresh_embeddings(conn)

    assert counts.embedded == 1
    assert _vector_ids(conn) == {post}
    assert child not in _vector_ids(conn) and stub not in _vector_ids(conn)
    assert empty not in _vector_ids(conn)
    model, recipe, digest = conn.execute(
        "SELECT model, recipe, text_hash FROM embedding_meta WHERE item_id = ?", (post,)
    ).fetchone()
    assert (model, recipe) == (emb.MODEL_NAME, emb.RECIPE)
    assert digest == emb.text_hash("A post Its text")


def test_a_second_refresh_embeds_nothing(conn):
    _add(conn, "a", "A post", "Its text")
    emb.refresh_embeddings(conn)
    CALLS.clear()

    counts = emb.refresh_embeddings(conn)

    assert (counts.embedded, counts.removed) == (0, 0)
    assert CALLS == []


def test_changed_text_is_embedded_again(conn):
    post = _add(conn, "a", "A post", "Its text")
    emb.refresh_embeddings(conn)
    conn.execute("UPDATE items SET content = 'New text' WHERE id = ?", (post,))
    conn.commit()

    counts = emb.refresh_embeddings(conn)

    assert counts.embedded == 1
    assert CALLS[-1] == ["A post New text"]


def test_a_change_past_the_recipe_cut_is_not_embedded_again(conn):
    post = _add(conn, "a", "A post", "x" * emb.CONTENT_CHARS)
    emb.refresh_embeddings(conn)
    conn.execute("UPDATE items SET content = content || 'more' WHERE id = ?", (post,))
    conn.commit()

    assert emb.refresh_embeddings(conn).embedded == 0


def test_another_model_or_recipe_is_embedded_again(conn, monkeypatch):
    _add(conn, "a", "A post", "Its text")
    emb.refresh_embeddings(conn)

    monkeypatch.setattr(emb, "RECIPE", "another-recipe")

    assert emb.refresh_embeddings(conn).embedded == 1


def test_a_vector_without_a_record_is_embedded_again(conn):
    # Vectors made before the record existed (#221)
    post = _add(conn, "a", "A post", "Its text")
    emb.ensure_vec_table(conn)
    blob = emb._serialize_embedding(_fake_vector("old"))
    conn.execute("INSERT INTO vec_items (item_id, embedding) VALUES (?, ?)", (post, blob))
    conn.commit()

    assert emb.refresh_embeddings(conn).embedded == 1
    assert conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0] == 1


def test_vectors_of_items_that_should_have_none_are_removed(conn):
    gone = _add(conn, "a", "Deleted", "text")
    stubbed = _add(conn, "b", "Becomes a stub", "text")
    moved = _add(conn, "c", "Becomes a child", "text")
    emptied = _add(conn, "d", "", "text")
    kept = _add(conn, "e", "Kept", "text")
    emb.refresh_embeddings(conn)
    assert _vector_ids(conn) == {gone, stubbed, moved, emptied, kept}

    conn.execute("DELETE FROM items WHERE id = ?", (gone,))
    conn.execute("UPDATE items SET held = 0 WHERE id = ?", (stubbed,))
    conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (kept, moved))
    conn.execute("UPDATE items SET content = '' WHERE id = ?", (emptied,))
    conn.commit()

    counts = emb.refresh_embeddings(conn)

    assert counts.removed == 4
    assert _vector_ids(conn) == {kept}
    assert [r[0] for r in conn.execute("SELECT item_id FROM embedding_meta")] == [kept]


def test_the_budget_stops_between_batches_and_the_next_run_goes_on(conn, monkeypatch):
    monkeypatch.setattr(emb, "BATCH_SIZE", 2)
    for n in range(5):
        _add(conn, f"s{n}", f"Item {n}", "text")
    clock = iter([0.0, 0.0, 10.0])  # the start, then before each batch
    monkeypatch.setattr(emb.time, "monotonic", lambda: next(clock))

    counts = emb.refresh_embeddings(conn, budget_s=5)

    assert (counts.embedded, counts.pending, counts.stopped) == (2, 3, True)
    monkeypatch.setattr(emb.time, "monotonic", lambda: 0.0)
    assert emb.refresh_embeddings(conn).embedded == 3


def test_newest_items_are_embedded_first(conn, monkeypatch):
    monkeypatch.setattr(emb, "BATCH_SIZE", 1)
    _add(conn, "old", "Old", "text")
    _add(conn, "new", "New", "text")

    emb.refresh_embeddings(conn)

    assert CALLS[0] == ["New text"]


def test_re_embed_item_records_its_vector_and_drops_it_when_emptied(conn):
    post = _add(conn, "a", "A post", "Its text")

    assert emb.re_embed_item(conn, post) is True
    assert _vector_ids(conn) == {post}

    conn.execute("UPDATE items SET title = NULL, content = '' WHERE id = ?", (post,))
    conn.commit()

    assert emb.re_embed_item(conn, post) is False
    assert _vector_ids(conn) == set()
    assert conn.execute("SELECT COUNT(*) FROM embedding_meta").fetchone()[0] == 0


def test_upsert_replaces_a_vector(conn):
    post = _add(conn, "a", "A post", "Its text")
    emb.ensure_vec_table(conn)
    emb.upsert_embedding(conn, post, _fake_vector("one"), "one")
    emb.upsert_embedding(conn, post, _fake_vector("two"), "two")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0] == 1
    digest = conn.execute("SELECT text_hash FROM embedding_meta").fetchone()[0]
    assert digest == emb.text_hash("two")


def test_stats_count_items_without_a_current_vector(conn):
    _add(conn, "a", "A post", "Its text")
    _add(conn, "b", "Another", "text")
    emb.ensure_vec_table(conn)
    assert emb.get_embedding_stats(conn)["out_of_date"] == 2

    emb.refresh_embeddings(conn)

    stats = emb.get_embedding_stats(conn)
    assert (stats["embedded"], stats["out_of_date"], stats["recipe"]) == (2, 0, emb.RECIPE)


def test_an_item_deleted_during_a_refresh_is_skipped(conn, monkeypatch):
    # The list is read first; an item can go while its batch is embedded (r1.2)
    first = _add(conn, "a", "First", "text")
    second = _add(conn, "b", "Second", "text")
    real = emb.embed_batch

    def batch_then_delete(texts):
        conn.execute("DELETE FROM items WHERE id = ?", (first,))
        conn.commit()
        return real(texts)

    monkeypatch.setattr(emb, "embed_batch", batch_then_delete)

    counts = emb.refresh_embeddings(conn)

    assert (counts.embedded, counts.pending) == (1, 0)
    assert _vector_ids(conn) == {second}

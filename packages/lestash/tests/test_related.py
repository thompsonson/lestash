"""Related items from vectors (#219): suggested across sources, once per pair, never
again once decided, and scanned again only when a vector changes."""

import json
import math

import lestash.core.embeddings as emb
import pytest
from lestash.core import related
from lestash.core.database import get_connection
from lestash.core.links import reject_link

TEXT = "A text long enough to be compared with the others, at sixty characters or more."


@pytest.fixture
def conn(test_db):
    with get_connection(test_db) as conn:
        emb.load_vec_extension(conn)
        emb.ensure_vec_table(conn)
        yield conn


def _vector(similarity: float, axis: int = 1) -> list[float]:
    """A unit vector whose cosine similarity with the first axis is ``similarity``."""
    v = [0.0] * emb.EMBEDDING_DIM
    v[0] = similarity
    v[axis] = math.sqrt(1 - similarity**2)
    return v


def _item(conn, source, similarity, *, axis=1, content=TEXT, title="An item"):
    item_id = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content) VALUES (?, ?, ?, ?)",
        (source, f"{source}-{similarity}-{axis}-{content[:5]}", title, content),
    ).lastrowid
    emb.upsert_embedding(conn, item_id, _vector(similarity, axis), content)
    conn.commit()
    return item_id


def _pairs(conn, status="suggested"):
    return {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT from_id, to_id FROM item_links WHERE kind = 'related' AND status = ?",
            (status,),
        )
    }


def _found_by(conn, item_id):
    """The items paired with this one by its own scan."""
    return {
        r[1] if r[0] == item_id else r[0]
        for r in conn.execute(
            "SELECT from_id, to_id FROM item_links l WHERE kind = 'related' "
            "AND status = 'suggested' AND EXISTS (SELECT 1 FROM json_each(l.evidence, "
            "'$.found_by') WHERE value = ?)",
            (item_id,),
        )
    }


def test_suggests_close_items_from_other_sources_only(conn):
    anchor = _item(conn, "kobo", 1.0)
    close = _item(conn, "arxiv", 0.9, axis=2)
    same_source = _item(conn, "kobo", 0.95, axis=3)
    far = _item(conn, "youtube", 0.3, axis=4)

    related.relate(conn)

    pairs = _pairs(conn)
    assert (anchor, close) in pairs
    assert not any(same_source in p and anchor in p for p in pairs)
    assert not any(far in p for p in pairs)


def test_a_pair_is_stored_once_lower_id_first_with_its_evidence(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)

    related.relate(conn)

    rows = conn.execute(
        "SELECT from_id, to_id, origin, evidence FROM item_links WHERE kind = 'related'"
    ).fetchall()
    assert len(rows) == 1
    from_id, to_id, origin, evidence = rows[0]
    assert (from_id, to_id) == (min(a, b), max(a, b)) and origin == related.ORIGIN
    found = json.loads(evidence)
    assert found["score"] == pytest.approx(0.9, abs=0.001)
    assert found["model"] == emb.MODEL_NAME and found["found_by"] == [a, b]


def test_pairs_linked_by_another_kind_are_skipped(conn):
    a = _item(conn, "microblog", 1.0)
    b = _item(conn, "bluesky", 0.99, axis=2)
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
        "VALUES (?, ?, 'syndication', 'linker:url', 'confirmed')",
        (b, a),
    )
    conn.commit()

    related.relate(conn)

    assert _pairs(conn) == set()


def test_at_most_five_from_each_items_own_scan(conn):
    anchor = _item(conn, "kobo", 1.0)
    for n in range(8):
        _item(conn, f"source{n}", 0.9 - n / 100, axis=n + 2)

    related.relate(conn)

    assert len(_found_by(conn, anchor)) == related.PER_ITEM


def test_decided_pairs_are_kept_and_never_suggested_again(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)
    c = _item(conn, "youtube", 0.85, axis=3)
    related.relate(conn)
    related.decide(conn, b, a, "rejected")
    related.decide(conn, a, c, "confirmed")
    conn.commit()

    related.relate(conn, everything=True)

    assert _pairs(conn, "rejected") == {(a, b)}
    assert _pairs(conn, "confirmed") == {(a, c)}
    assert not _pairs(conn) & {(a, b), (a, c)}
    decided = conn.execute(
        "SELECT COUNT(*) FROM item_links WHERE kind = 'related' AND decided_at IS NOT NULL"
    ).fetchone()[0]
    assert decided == 2


def test_only_items_whose_vector_changed_are_scanned_again(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)
    assert related.relate(conn).scanned == 2
    assert related.relate(conn).scanned == 0

    # b's text changed: its vector now points away from a
    emb.upsert_embedding(conn, b, _vector(0.1, 2), "changed")
    conn.execute(
        "UPDATE embedding_meta SET embedded_at = '2099-01-01 00:00:00' WHERE item_id = ?", (b,)
    )
    conn.commit()
    counts = related.relate(conn)

    assert counts.scanned == 1
    # a's own scan still finds the pair, until a's vector changes
    assert _found_by(conn, a) == {b} and _found_by(conn, b) == set()


def test_an_item_no_longer_compared_loses_its_suggestions(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)
    related.relate(conn)
    assert _pairs(conn)

    conn.execute("UPDATE items SET held = 0 WHERE id = ?", (b,))
    conn.commit()
    counts = related.relate(conn)

    assert counts.dropped == 1 and _pairs(conn) == set()
    assert related.suggestions_for(conn, a) == []


def test_short_texts_are_left_out(conn):
    _item(conn, "kobo", 1.0, content="this !", title="")
    _item(conn, "arxiv", 0.99, axis=2, content="this", title="")

    related.relate(conn)

    assert _pairs(conn) == set()


def test_nothing_runs_while_a_new_index_is_built(conn, monkeypatch):
    _item(conn, "kobo", 1.0)
    _item(conn, "arxiv", 0.9, axis=2)
    monkeypatch.setattr(related, "building", lambda conn: True)

    counts = related.relate(conn)

    assert counts.building and counts.scanned == 0 and _pairs(conn) == set()


def test_suggestions_best_first_and_rejected_with(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.8, axis=2)
    c = _item(conn, "youtube", 0.95, axis=3)
    related.relate(conn)

    assert [row["id"] for row, _ in related.suggestions_for(conn, a)] == [c, b]

    related.decide(conn, a, b, "rejected")
    assert related.rejected_with(conn, a) == {b}
    assert related.rejected_with(conn, b) == {a}


def test_decide_refuses_an_item_with_itself(conn):
    a = _item(conn, "kobo", 1.0)
    with pytest.raises(ValueError):
        related.decide(conn, a, a, "confirmed")


def test_removing_a_link_records_when(conn):
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)
    related.decide(conn, a, b, "confirmed")
    conn.commit()

    assert reject_link(conn, min(a, b), max(a, b), "related")

    status, decided = conn.execute(
        "SELECT status, decided_at FROM item_links WHERE kind = 'related'"
    ).fetchone()
    assert status == "rejected" and decided


def test_decided_pairs_dont_use_up_an_items_five(conn):
    """Rejected and confirmed pairs are passed over: the item still gets five
    suggestions (review round 1)."""
    anchor = _item(conn, "kobo", 1.0)
    others = [_item(conn, f"source{n}", 0.95 - n / 100, axis=n + 2) for n in range(7)]
    related.decide(conn, anchor, others[0], "rejected")
    related.decide(conn, anchor, others[1], "confirmed")
    conn.commit()

    related.relate(conn)

    assert _found_by(conn, anchor) == set(others[2:7])


def test_a_pair_both_scans_found_stays_until_neither_does(conn):
    """The pair counts for both items, and goes only when neither scan finds it
    (review round 2)."""
    a = _item(conn, "kobo", 1.0)
    b = _item(conn, "arxiv", 0.9, axis=2)
    related.relate(conn)
    assert _found_by(conn, a) == {b} and _found_by(conn, b) == {a}

    _change(conn, a, _vector(0.1, 1))
    related.relate(conn)
    # b's vector hasn't changed: its scan still holds the pair
    assert _pairs(conn) == {(min(a, b), max(a, b))} and _found_by(conn, a) == set()

    _change(conn, b, _vector(0.1, 2))
    related.relate(conn)
    assert _pairs(conn) == set()


def _change(conn, item_id, vector):
    emb.upsert_embedding(conn, item_id, vector, f"changed {item_id}")
    conn.execute(
        "UPDATE embedding_meta SET embedded_at = datetime('now', '+1 day', ?) WHERE item_id = ?",
        (f"+{item_id} seconds", item_id),
    )
    conn.commit()

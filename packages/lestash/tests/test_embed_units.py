"""What gets a vector, and its text (#221, step 2): rounds for chats, comments with
their post, highlights with their book, nothing for reactions or watch copies."""

import hashlib
import json

import lestash.core.embeddings as emb
import pytest
from lestash.core import embed_units as units
from lestash.core.database import get_connection


def _fake_vector(text: str) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    vector = [b / 255 for b in (seed * 12)[: emb.EMBEDDING_DIM]]
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector]


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(emb, "embed_batch", lambda texts: [_fake_vector(t) for t in texts])
    monkeypatch.setattr(emb, "embed_text", _fake_vector)


@pytest.fixture
def conn(test_db):
    with get_connection(test_db) as conn:
        emb.load_vec_extension(conn)
        yield conn


_n = iter(range(10**6))


def _add(conn, source_type, title, content, *, parent=None, at=None, **metadata):
    item_id = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content, parent_id, created_at,"
        " metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            source_type,
            metadata.pop("source_id", f"s{next(_n)}"),
            title,
            content,
            parent,
            at,
            json.dumps(metadata) if metadata else None,
        ),
    ).lastrowid
    conn.commit()
    return item_id


def _units(conn, families=None):
    return {u.item_id: u for u in units.embedding_units(conn, families)}


def test_a_chat_is_embedded_by_round(conn):
    chat = _add(conn, "claude", "Bayes for beginners", "first prompt")
    ask = _add(
        conn, "claude", None, "What is a prior?", parent=chat, at="2026-01-01 10:00", sender="human"
    )
    reply = _add(
        conn,
        "claude",
        None,
        "A belief before data.",
        parent=chat,
        at="2026-01-01 10:01",
        sender="assistant",
    )
    ask2 = _add(
        conn, "claude", None, "And a posterior?", parent=chat, at="2026-01-01 10:02", sender="human"
    )
    more = _add(
        conn, "claude", None, "After data.", parent=chat, at="2026-01-01 10:03", sender="assistant"
    )
    also = _add(
        conn,
        "claude",
        None,
        "Bayes updates one to the other.",
        parent=chat,
        at="2026-01-01 10:04",
        sender="assistant",
    )

    found = _units(conn)

    assert found[ask].recipe == units.ROUND_RECIPE
    assert found[ask].text == "Bayes for beginners\nWhat is a prior?\nA belief before data."
    assert found[ask2].text == (
        "Bayes for beginners\nAnd a posterior?\nAfter data. Bayes updates one to the other."
    )
    # A reply is part of its round, not a vector of its own
    assert reply not in found and more not in found and also not in found
    assert found[chat].recipe == units.ITEM_RECIPE


def test_turns_with_the_same_time_keep_their_import_order(conn):
    chat = _add(conn, "claude", "Chat", "x")
    ask = _add(conn, "claude", None, "Question", parent=chat, at="2026-01-01 10:00", sender="human")
    _add(conn, "claude", None, "Answer", parent=chat, at="2026-01-01 10:00", sender="assistant")

    assert _units(conn)[ask].text == "Chat\nQuestion\nAnswer"


def test_a_gemini_reply_goes_with_the_prompt_it_names(conn):
    chat = _add(conn, "gemini", "Trip", "x")
    first = _add(
        conn,
        "gemini",
        None,
        "Trains to Lyon?",
        parent=chat,
        at="2026-01-01 10:00",
        role="user",
        source_id="g-1",
    )
    second = _add(
        conn,
        "gemini",
        None,
        "Hotels in Lyon?",
        parent=chat,
        at="2026-01-01 10:01",
        role="user",
        source_id="g-2",
    )
    # Imported late, after the second prompt, but it answers the first
    _add(
        conn,
        "gemini",
        None,
        "The TGV, two hours.",
        parent=chat,
        at="2026-01-01 10:02",
        role="model",
        prompt_source_id="g-1",
    )

    found = _units(conn)

    assert found[first].text == "Trip\nTrains to Lyon?\nThe TGV, two hours."
    assert found[second].text == "Trip\nHotels in Lyon?"


def test_replies_before_any_prompt_make_a_round(conn):
    chat = _add(conn, "mistral", "Chat", "x")
    greet = _add(
        conn,
        "mistral",
        None,
        "Hello, how can I help?",
        parent=chat,
        at="2026-01-01 10:00",
        role="assistant",
    )

    assert _units(conn)[greet].text == "Chat\nHello, how can I help?"


def test_long_rounds_are_cut_per_side(conn):
    chat = _add(conn, "claude", "T", "x")
    ask = _add(conn, "claude", None, "p" * 900, parent=chat, at="2026-01-01 10:00", sender="human")
    _add(conn, "claude", None, "r" * 900, parent=chat, at="2026-01-01 10:01", sender="assistant")

    text = _units(conn)[ask].text

    assert text == "T\n" + "p" * units.PROMPT_CHARS + "\n" + "r" * units.REPLY_CHARS


def test_a_comment_carries_its_post_and_a_highlight_its_book(conn):
    post = _add(conn, "bluesky", None, "Priors matter more than you think")
    comment = _add(conn, "bluesky", None, "Agreed, in small samples.", parent=post)
    book = _add(conn, "kobo", "Thinking in Bets", "", kind="book")
    mark = _add(conn, "kobo", None, "Decisions are bets.", parent=book, kind="highlight")

    found = _units(conn)

    assert found[comment].recipe == units.COMMENT_RECIPE
    assert found[comment].text == "Priors matter more than you think\nAgreed, in small samples."
    assert found[mark].recipe == units.HIGHLIGHT_RECIPE
    assert found[mark].text == "Thinking in Bets\nDecisions are bets."


def test_reactions_and_watch_copies_get_no_vector(conn):
    post = _add(conn, "linkedin", "A post", "text", resource_name="ugcPosts")
    like = _add(conn, "linkedin", None, "liked", parent=post, resource_name="socialActions/likes")
    video = _add(conn, "youtube", "A talk", "liked")
    watch = _add(conn, "youtube", "A talk", "watched", parent=video)
    transcript = _add(
        conn, "youtube", "A talk", "Hello and welcome", parent=video, type="transcript"
    )

    found = _units(conn)

    assert like not in found and watch not in found
    assert found[transcript].recipe == units.ITEM_RECIPE


def test_families_limit_the_units(conn):
    chat = _add(conn, "claude", "Chat", "x")
    ask = _add(conn, "claude", None, "Q", parent=chat, sender="human")
    other = _add(conn, "note", "Other", "text")

    assert set(_units(conn, {chat})) == {chat, ask}
    assert set(_units(conn, {other})) == {other}


def test_editing_a_reply_re_embeds_its_round(conn):
    chat = _add(conn, "claude", "Chat", "x")
    ask = _add(conn, "claude", None, "Question", parent=chat, at="2026-01-01 10:00", sender="human")
    reply = _add(
        conn, "claude", None, "Answer", parent=chat, at="2026-01-01 10:01", sender="assistant"
    )
    emb.refresh_embeddings(conn)
    conn.execute("UPDATE items SET content = 'Better answer' WHERE id = ?", (reply,))
    conn.commit()

    # The reply has no vector of its own; its round, on the prompt, is redone
    assert emb.re_embed_item(conn, reply) is False
    digest = conn.execute("SELECT text_hash FROM embedding_meta WHERE item_id = ?", (ask,))
    assert digest.fetchone()[0] == emb.text_hash("Chat\nQuestion\nBetter answer")


def test_renaming_a_post_re_embeds_its_comments(conn):
    post = _add(conn, "bluesky", None, "Old start")
    comment = _add(conn, "bluesky", None, "A reply", parent=post)
    emb.refresh_embeddings(conn)
    conn.execute("UPDATE items SET title = 'New title' WHERE id = ?", (post,))
    conn.commit()

    assert emb.re_embed_item(conn, post) is True
    digest = conn.execute("SELECT text_hash FROM embedding_meta WHERE item_id = ?", (comment,))
    assert digest.fetchone()[0] == emb.text_hash("New title\nA reply")


def test_a_turn_that_stops_starting_a_round_loses_its_vector(conn):
    chat = _add(conn, "claude", "Chat", "x")
    first = _add(conn, "claude", None, "Q", parent=chat, at="2026-01-01 10:00", sender="human")
    second = _add(conn, "claude", None, "Q2", parent=chat, at="2026-01-01 10:01", sender="human")
    emb.refresh_embeddings(conn)
    assert {first, second} <= {r[0] for r in conn.execute("SELECT item_id FROM vec_items")}

    # Re-imported as a reply: it now belongs to the first round
    conn.execute('UPDATE items SET metadata = \'{"sender": "assistant"}\' WHERE id = ?', (second,))
    conn.commit()
    counts = emb.refresh_embeddings(conn)

    assert counts.removed == 1
    assert second not in {r[0] for r in conn.execute("SELECT item_id FROM vec_items")}


def test_stats_count_parts_apart(conn):
    chat = _add(conn, "claude", "Chat", "x")
    _add(conn, "claude", None, "Q", parent=chat, sender="human")
    emb.refresh_embeddings(conn)

    stats = emb.get_embedding_stats(conn)

    assert (stats["embedded"], stats["parts_embedded"], stats["out_of_date"]) == (1, 1, 0)


def test_a_gemini_reply_sorted_before_its_prompt_still_joins_it(conn):
    # Review round 1 (r1.1): the prompt it names comes later in time order
    chat = _add(conn, "gemini", "Trip", "x")
    _add(
        conn,
        "gemini",
        None,
        "The TGV.",
        parent=chat,
        at="2026-01-01 09:59",
        role="model",
        prompt_source_id="g-9",
    )
    ask = _add(
        conn,
        "gemini",
        None,
        "Trains to Lyon?",
        parent=chat,
        at="2026-01-01 10:00",
        role="user",
        source_id="g-9",
    )

    found = _units(conn)

    assert found[ask].text == "Trip\nTrains to Lyon?\nThe TGV."
    assert len(found) == 2  # the chat and its one round

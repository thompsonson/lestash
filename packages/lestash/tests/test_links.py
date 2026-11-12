"""Typed links between items: the addresses in content that name another item."""

from __future__ import annotations

import json
import sqlite3

from lestash.core.database import get_connection
from lestash.core.links import address_key, link_items, links_of, reject_link
from lestash.core.sync import SyncStore
from lestash.models.item import ItemCreate


def _add(conn: sqlite3.Connection, source_type: str, source_id: str, **fields) -> int:
    fields.setdefault("content", "")
    metadata = fields.pop("metadata", None)
    columns = ["source_type", "source_id", "metadata", *fields]
    values = [source_type, source_id, json.dumps(metadata) if metadata else None, *fields.values()]
    return conn.execute(
        f"INSERT INTO items ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))}) "
        "RETURNING id",
        values,
    ).fetchone()[0]


def _links(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT from_id, to_id, kind, evidence, status FROM item_links ORDER BY from_id, to_id"
        )
    ]


def test_address_keys_match_the_forms_of_one_address() -> None:
    assert address_key("https://www.example.com/a/") == address_key("http://example.com/a#x")
    assert address_key("https://example.com/a.") == address_key("https://example.com/a")
    video = "youtube:aOHNWiy93gQ"
    assert address_key("https://youtu.be/aOHNWiy93gQ?si=abc") == video
    assert address_key("https://www.youtube.com/watch?v=aOHNWiy93gQ&t=10") == video
    assert address_key("https://youtube.com/shorts/aOHNWiy93gQ?is=x") == video
    assert address_key("https://") is None


def test_an_address_in_content_links_to_the_item_there(test_db) -> None:
    with get_connection(test_db) as conn:
        post = _add(conn, "microblog", "p1", url="https://matt.example/2026/01/18/post.html")
        chat = _add(
            conn,
            "claude",
            "c1",
            content="See https://matt.example/2026/01/18/post.html, and again "
            "https://matt.example/2026/01/18/post.html. Also https://nowhere.example/x",
        )
        assert link_items(conn) == 1
        assert link_items(conn) == 0  # a rerun adds nothing
        assert _links(conn) == [
            (chat, post, "references", "https://matt.example/2026/01/18/post.html", "confirmed")
        ]
        [outgoing] = links_of(conn, chat)
        [incoming] = links_of(conn, post)
    assert (outgoing["direction"], outgoing["item"]["id"]) == ("outgoing", post)
    assert (incoming["direction"], incoming["item"]["id"]) == ("incoming", chat)


def test_a_video_address_links_to_the_canonical_video_item(test_db) -> None:
    url = "https://www.youtube.com/watch?v=aOHNWiy93gQ"
    with get_connection(test_db) as conn:
        _add(conn, "youtube", "liked:aOHNWiy93gQ", url=url)
        history = _add(conn, "youtube", "history:aOHNWiy93gQ", url=url)
        note = _add(conn, "gemini", "g1", content="Watch https://youtu.be/aOHNWiy93gQ?si=abc")
        # An item naming its own address links nothing
        _add(conn, "web", "w1", url="https://self.example/", content="https://self.example/")
        link_items(conn)
        assert [(r[0], r[1]) for r in _links(conn)] == [(note, history)]


def test_a_bluesky_cross_post_is_a_syndication_of_the_micro_blog_post(test_db) -> None:
    url = "https://matt.example/2026/01/20/wholesome.html"
    with get_connection(test_db) as conn:
        original = _add(
            conn,
            "microblog",
            "p2",
            url=url,
            created_at="2026-01-20 10:00:00+00:00",
            metadata={"syndication": ["mastodon", "bluesky", "linkedin"]},
        )
        copy = _add(
            conn,
            "bluesky",
            "b1",
            content=f"So wholesome {url}",
            created_at="2026-01-20T10:02:00Z",
            is_own_content=1,
        )
        # Your reply to someone (its parent isn't stored), someone else's post at the
        # same time, and your LinkedIn comment
        answer = _add(
            conn,
            "bluesky",
            "b4",
            content=f"I wrote {url}",
            created_at="2026-01-20T10:03:00Z",
            is_own_content=1,
            metadata={"reply_to": {"uri": "at://someone/post/1"}},
        )
        theirs = _add(
            conn, "bluesky", "b3", content=f"Look: {url}", created_at="2026-01-20T10:03:00Z"
        )
        comment = _add(
            conn,
            "linkedin",
            "changelog-socialActions/comments-1",
            content=f"See {url}",
            created_at="2026-01-20 11:04:00",
            is_own_content=1,
        )
        # A reply, a post a day later, and a service not in the post's syndication only
        # reference it
        reply = _add(
            conn,
            "bluesky",
            "b2",
            content=f"Re {url}",
            created_at="2026-01-20T10:05:00Z",
            parent_id=copy,
        )
        later = _add(
            conn, "linkedin", "l1", content=f"I wrote {url}", created_at="2026-01-21 10:00:00"
        )
        other = _add(
            conn, "microblog", "p9", content=f"As I wrote {url}", created_at="2026-01-20 10:01:00"
        )
        link_items(conn)
        assert [(r[0], r[1], r[2]) for r in _links(conn)] == [
            (original, copy, "syndication"),
            (answer, original, "references"),
            (theirs, original, "references"),
            (comment, original, "references"),
            (reply, original, "references"),
            (later, original, "references"),
            (other, original, "references"),
        ]


def test_a_linkedin_reaction_or_comment_is_never_a_link_target(test_db) -> None:
    url = "https://www.linkedin.com/feed/update/urn:li:activity:1"
    with get_connection(test_db) as conn:
        _add(conn, "linkedin", "changelog-socialActions/likes-1", url=url)
        _add(conn, "linkedin", "changelog-socialActions/comments-1", url=url)
        _add(conn, "claude", "c5", content=f"See {url}")
        assert link_items(conn) == 0


def test_a_failed_linker_leaves_the_sync_done(test_db, monkeypatch) -> None:
    def fail(conn):
        conn.execute(
            "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
            "VALUES (1, 1, 'x', 'test', 'confirmed')"
        )
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("lestash.core.sync.link_items", fail)
    with get_connection(test_db) as conn:
        store = SyncStore(conn, "bluesky")
        store.run(iter([ItemCreate(source_type="bluesky", source_id="b6", content="x")]))
        assert store.counts.added == 1
        assert _links(conn) == []  # the failed pass's writes are rolled back


def test_a_removed_link_stays_removed(test_db) -> None:
    with get_connection(test_db) as conn:
        post = _add(conn, "microblog", "p3", url="https://matt.example/p3")
        chat = _add(conn, "claude", "c3", content="https://matt.example/p3")
        link_items(conn)
        assert reject_link(conn, chat, post, "references")
        assert not reject_link(conn, post, chat, "references")
        assert link_items(conn) == 0
        assert links_of(conn, chat) == []
        assert _links(conn)[0][-1] == "rejected"
        # Deleting an item deletes its links
        conn.execute("DELETE FROM items WHERE id = ?", (post,))
        assert _links(conn) == []


def test_a_sync_that_writes_items_links_them(test_db) -> None:
    with get_connection(test_db) as conn:
        post = _add(conn, "microblog", "p4", url="https://matt.example/p4")
        conn.commit()
        item = ItemCreate(source_type="bluesky", source_id="b4", content="https://matt.example/p4")
        SyncStore(conn, "bluesky").run(iter([item]))
        assert [r[1] for r in _links(conn)] == [post]


def test_a_linkedin_cross_post_is_a_syndication(test_db) -> None:
    """LinkedIn's created_at is UTC since migration 26 (#236), like every source's."""
    url = "https://matt.example/2026/01/20/post.html"
    with get_connection(test_db) as conn:
        original = _add(
            conn,
            "microblog",
            "p7",
            url=url,
            created_at="2026-01-20 10:00:00+00:00",
            metadata={"syndication": ["linkedin"]},
        )
        copy = _add(
            conn,
            "linkedin",
            "changelog-ugcPosts-urn:li:share:7",
            content=f"New post {url}",
            created_at="2026-01-20 10:30:00+00:00",
            is_own_content=1,
        )
        link_items(conn)
        assert [(r[0], r[1], r[2]) for r in _links(conn)] == [(original, copy, "syndication")]

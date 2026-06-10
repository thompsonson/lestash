"""Tests for feed-preview caching (post_cache enrichment from public OG previews)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from lestash_linkedin import feed_preview
from lestash_linkedin.feed_preview import (
    _extract_author,
    _urn_to_fetchable_id,
    build_worklist,
    cache_engaged_posts,
)

ITEMS_DDL = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT,
    content TEXT NOT NULL,
    is_own_content BOOLEAN DEFAULT FALSE,
    metadata TEXT
)
"""

# Mirrors the post_cache schema created by the core migrations.
POST_CACHE_DDL = """
CREATE TABLE post_cache (
    id INTEGER PRIMARY KEY,
    urn TEXT UNIQUE NOT NULL,
    author_urn TEXT,
    author_name TEXT,
    content_preview TEXT,
    full_content TEXT,
    image_path TEXT,
    url TEXT,
    created_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'manual',
    reactor_name TEXT
)
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(ITEMS_DDL + ";" + POST_CACHE_DDL)
    return c


def _add_engagement(
    conn: sqlite3.Connection, *, item_id: int, resource: str, key: str, target_urn: str
) -> None:
    """Insert an own-content engagement item (like/comment) targeting a URN."""
    metadata = {"resource_name": resource, key: target_urn}
    conn.execute(
        "INSERT INTO items (id, source_type, source_id, content, is_own_content, metadata) "
        "VALUES (?, 'linkedin', ?, ?, 1, ?)",
        (item_id, f"changelog-{resource}-{item_id}", "engagement", json.dumps(metadata)),
    )
    conn.commit()


def _ok_preview(desc: str = "A great post", author: str = "Jane Doe") -> dict:
    return {
        "status": "200",
        "title": f"... | {author} posted on the topic | LinkedIn",
        "author": author,
        "handle": "janedoe",
        "og_url": "https://www.linkedin.com/posts/janedoe_slug-activity-123-abc",
        "description": desc,
    }


# --- pure helpers -----------------------------------------------------------


def test_urn_to_fetchable_id_shapes() -> None:
    assert _urn_to_fetchable_id("urn:li:activity:123") == ("activity", "123")
    assert _urn_to_fetchable_id("urn:li:ugcPost:456") == ("ugcPost", "456")
    # Compound comment URN -> fetch the parent activity
    assert _urn_to_fetchable_id("urn:li:comment:(activity:789,1011)") == ("activity", "789")
    # groupPost and unknown shapes are unfetchable
    assert _urn_to_fetchable_id("urn:li:groupPost:12-34") is None


def test_extract_author_posted_on() -> None:
    title = "Some headline | Richard Bellman posted on the topic | LinkedIn"
    assert _extract_author(title, "rbellman") == "Richard Bellman"


def test_extract_author_repeated_pipe_segment() -> None:
    title = "A clever insight | Ada Lovelace | Ada Lovelace | 12 comments"
    assert _extract_author(title, None) == "Ada Lovelace"


def test_extract_author_falls_back_to_handle() -> None:
    assert _extract_author(None, "benedictevans") == "benedictevans"


# --- worklist ---------------------------------------------------------------


def test_build_worklist_collects_and_dedupes(conn: sqlite3.Connection) -> None:
    _add_engagement(
        conn,
        item_id=1,
        resource="socialActions/likes",
        key="reacted_to",
        target_urn="urn:li:activity:111",
    )
    _add_engagement(
        conn,
        item_id=2,
        resource="socialActions/comments",
        key="commented_on",
        target_urn="urn:li:activity:222",
    )
    # Already cached -> excluded from worklist
    conn.execute("INSERT INTO post_cache (urn, source) VALUES ('urn:li:activity:111', 'manual')")
    # Unfetchable URN -> counted as skipped, not fetched
    _add_engagement(
        conn,
        item_id=3,
        resource="socialActions/likes",
        key="reacted_to",
        target_urn="urn:li:groupPost:9-9",
    )
    conn.commit()

    worklist, scope = build_worklist(conn)

    assert [urn for urn, _ in worklist] == ["urn:li:activity:222"]
    assert scope["already_cached"] == 1
    assert scope["skipped_unfetchable"] == 1
    assert scope["to_fetch"] == 1


# --- cache_engaged_posts ----------------------------------------------------


def test_cache_writes_and_is_idempotent(conn, monkeypatch) -> None:
    _add_engagement(
        conn,
        item_id=1,
        resource="socialActions/likes",
        key="reacted_to",
        target_urn="urn:li:activity:111",
    )
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": _ok_preview()
    )

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["ok"] == 1

    row = conn.execute(
        "SELECT author_name, content_preview, source FROM post_cache WHERE urn=?",
        ("urn:li:activity:111",),
    ).fetchone()
    assert row["author_name"] == "Jane Doe"
    assert row["content_preview"] == "A great post"
    assert row["source"] == "feed_preview"

    # Re-run: nothing left to fetch
    stats2 = cache_engaged_posts(conn, sleep=0)
    assert stats2["to_fetch"] == 0
    assert stats2["ok"] == 0


def test_dry_run_does_not_write(conn, monkeypatch) -> None:
    _add_engagement(
        conn,
        item_id=1,
        resource="socialActions/likes",
        key="reacted_to",
        target_urn="urn:li:activity:111",
    )
    called = False

    def _spy(fid, urn_kind="activity"):
        nonlocal called
        called = True
        return _ok_preview()

    monkeypatch.setattr(feed_preview, "fetch_preview", _spy)

    stats = cache_engaged_posts(conn, dry_run=True, sleep=0)
    assert stats["to_fetch"] == 1
    assert called is False
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 0


def test_generic_fallback_not_cached(conn, monkeypatch) -> None:
    _add_engagement(
        conn,
        item_id=1,
        resource="socialActions/likes",
        key="reacted_to",
        target_urn="urn:li:activity:111",
    )
    # og_url without /posts/ means deleted/private -> generic LinkedIn home meta
    bad = _ok_preview()
    bad["og_url"] = "https://www.linkedin.com/"
    monkeypatch.setattr(feed_preview, "fetch_preview", lambda fid, urn_kind="activity": bad)

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["ok"] == 0
    assert stats["miss"] == 1
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 0


def test_stops_on_429(conn, monkeypatch) -> None:
    for i in range(3):
        _add_engagement(
            conn,
            item_id=i + 1,
            resource="socialActions/likes",
            key="reacted_to",
            target_urn=f"urn:li:activity:{i}",
        )
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": {"status": "429"}
    )

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["ok"] == 0
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 0


def test_limit_caps_fetches(conn, monkeypatch) -> None:
    for i in range(5):
        _add_engagement(
            conn,
            item_id=i + 1,
            resource="socialActions/likes",
            key="reacted_to",
            target_urn=f"urn:li:activity:{i}",
        )
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": _ok_preview()
    )

    stats = cache_engaged_posts(conn, limit=2, sleep=0)
    assert stats["fetched"] == 2
    assert stats["ok"] == 2
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 2

"""Tests for feed-preview caching (post_cache enrichment from public OG previews)."""

from __future__ import annotations

import json
import sqlite3
from urllib.error import HTTPError

import pytest
from lestash_linkedin import feed_preview
from lestash_linkedin.feed_preview import (
    GONE_SOURCE,
    _extract_author,
    _urn_to_fetchable_id,
    build_worklist,
    cache_engaged_posts,
    fetch_preview,
    run_during_sync,
)

ITEMS_DDL = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT,
    content TEXT NOT NULL,
    created_at DATETIME,
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


def _add_like(
    conn: sqlite3.Connection, *, item_id: int, target_urn: str, created_at: str = "2026-01-01"
) -> None:
    """Insert an own-content like targeting a URN."""
    metadata = {"resource_name": "socialActions/likes", "reacted_to": target_urn}
    conn.execute(
        "INSERT INTO items (id, source_type, source_id, content, created_at, "
        "is_own_content, metadata) VALUES (?, 'linkedin', ?, 'x', ?, 1, ?)",
        (item_id, f"like-{item_id}", created_at, json.dumps(metadata)),
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
        "error": None,
    }


# --- pure helpers -----------------------------------------------------------


def test_urn_to_fetchable_id_shapes() -> None:
    assert _urn_to_fetchable_id("urn:li:activity:123") == ("activity", "123")
    assert _urn_to_fetchable_id("urn:li:ugcPost:456") == ("ugcPost", "456")
    assert _urn_to_fetchable_id("urn:li:comment:(activity:789,1011)") == ("activity", "789")
    assert _urn_to_fetchable_id("urn:li:groupPost:12-34") is None


def test_extract_author_posted_on() -> None:
    title = "Some headline | Richard Bellman posted on the topic | LinkedIn"
    assert _extract_author(title, "rbellman") == "Richard Bellman"


def test_extract_author_repeated_pipe_segment() -> None:
    title = "A clever insight | Ada Lovelace | Ada Lovelace | 12 comments"
    assert _extract_author(title, None) == "Ada Lovelace"


def test_extract_author_falls_back_to_handle() -> None:
    assert _extract_author(None, "benedictevans") == "benedictevans"


# --- fetch_preview: HTTP error handling (High fix) --------------------------


def test_fetch_preview_surfaces_http_error_code(monkeypatch) -> None:
    """urlopen raises HTTPError for 4xx/5xx — fetch_preview must surface the code,
    not bury it as a generic 'ERR', so callers can act on 429s."""

    def _raise_429(*_a, **_k):
        raise HTTPError("https://x", 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(feed_preview, "urlopen", _raise_429)
    result = fetch_preview("123")
    assert result["status"] == "429"
    assert result["author"] is None


def test_fetch_preview_transport_error_is_err(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise OSError("connection reset")

    monkeypatch.setattr(feed_preview, "urlopen", _boom)
    assert fetch_preview("123")["status"] == "ERR"


# --- worklist ---------------------------------------------------------------


def test_build_worklist_collects_and_dedupes(conn: sqlite3.Connection) -> None:
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
    _add_like(conn, item_id=2, target_urn="urn:li:activity:222")
    # Already cached -> excluded
    conn.execute("INSERT INTO post_cache (urn, source) VALUES ('urn:li:activity:111', 'manual')")
    # Unfetchable URN -> counted as skipped
    _add_like(conn, item_id=3, target_urn="urn:li:groupPost:9-9")
    conn.commit()

    worklist, scope = build_worklist(conn)

    assert [urn for urn, _ in worklist] == ["urn:li:activity:222"]
    assert scope["already_cached"] == 1
    assert scope["skipped_unfetchable"] == 1
    assert scope["to_fetch"] == 1


def test_build_worklist_orders_newest_first(conn: sqlite3.Connection) -> None:
    """Newest engagement first, so a bounded run never starves fresh URNs."""
    _add_like(conn, item_id=1, target_urn="urn:li:activity:old", created_at="2026-01-01")
    _add_like(conn, item_id=2, target_urn="urn:li:activity:new", created_at="2026-06-01")
    _add_like(conn, item_id=3, target_urn="urn:li:activity:mid", created_at="2026-03-01")

    worklist, _ = build_worklist(conn)
    assert [urn for urn, _ in worklist] == [
        "urn:li:activity:new",
        "urn:li:activity:mid",
        "urn:li:activity:old",
    ]


# --- cache_engaged_posts ----------------------------------------------------


def test_cache_writes_and_is_idempotent(conn, monkeypatch) -> None:
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
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

    stats2 = cache_engaged_posts(conn, sleep=0)
    assert stats2["to_fetch"] == 0
    assert stats2["ok"] == 0


def test_dry_run_does_not_write(conn, monkeypatch) -> None:
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
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


def test_gone_post_is_negative_cached_and_not_refetched(conn, monkeypatch) -> None:
    """Deleted/private posts (generic OG fallback) get a sentinel row so they're
    not re-scraped on every run (Medium fix)."""
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
    bad = _ok_preview()
    bad["og_url"] = "https://www.linkedin.com/"  # no /posts/ -> generic home meta

    calls = 0

    def _fetch(fid, urn_kind="activity"):
        nonlocal calls
        calls += 1
        return bad

    monkeypatch.setattr(feed_preview, "fetch_preview", _fetch)

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["miss"] == 1 and stats["ok"] == 0
    row = conn.execute(
        "SELECT source, content_preview FROM post_cache WHERE urn=?", ("urn:li:activity:111",)
    ).fetchone()
    assert row["source"] == GONE_SOURCE
    assert row["content_preview"] is None

    # Second run: sentinel keeps it out of the worklist; no re-fetch.
    stats2 = cache_engaged_posts(conn, sleep=0)
    assert stats2["to_fetch"] == 0
    assert calls == 1


def test_transient_error_is_not_cached_and_retried(conn, monkeypatch) -> None:
    """A transport/HTTP error must NOT be negative-cached — it should retry next run."""
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
    monkeypatch.setattr(
        feed_preview,
        "fetch_preview",
        lambda fid, urn_kind="activity": {"status": "ERR", "error": "boom", "og_url": None},
    )

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["err"] == 1
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 0
    # Still pending next run.
    _, scope = build_worklist(conn)
    assert scope["to_fetch"] == 1


def test_stops_on_429(conn, monkeypatch) -> None:
    for i in range(3):
        _add_like(conn, item_id=i + 1, target_urn=f"urn:li:activity:{i}")
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": {"status": "429"}
    )

    stats = cache_engaged_posts(conn, sleep=0)
    assert stats["ok"] == 0
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 0


def test_limit_caps_fetches(conn, monkeypatch) -> None:
    for i in range(5):
        _add_like(conn, item_id=i + 1, target_urn=f"urn:li:activity:{i}")
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": _ok_preview()
    )

    stats = cache_engaged_posts(conn, limit=2, sleep=0)
    assert stats["fetched"] == 2
    assert stats["ok"] == 2
    assert conn.execute("SELECT COUNT(*) FROM post_cache").fetchone()[0] == 2


def test_kinds_filter_restricts_collection(conn, monkeypatch) -> None:
    _add_like(conn, item_id=1, target_urn="urn:li:activity:111")
    # A comment target that should be ignored when kinds=reacted_to only
    conn.execute(
        "INSERT INTO items (id, source_type, source_id, content, created_at, is_own_content, "
        "metadata) VALUES (2, 'linkedin', 'c-2', 'x', '2026-01-01', 1, ?)",
        (
            json.dumps(
                {"resource_name": "socialActions/comments", "commented_on": "urn:li:activity:222"}
            ),
        ),
    )
    conn.commit()
    monkeypatch.setattr(
        feed_preview, "fetch_preview", lambda fid, urn_kind="activity": _ok_preview()
    )

    stats = cache_engaged_posts(conn, kinds=("reacted_to",), sleep=0)
    assert stats["ok"] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM post_cache WHERE urn='urn:li:activity:222'").fetchone()[
            0
        ]
        == 0
    )


# --- run_during_sync (sync-hook integration) --------------------------------


def test_run_during_sync_disabled_skips(conn, monkeypatch) -> None:
    monkeypatch.setattr(
        feed_preview,
        "cache_engaged_posts",
        lambda *a, **k: pytest.fail("should not run when disabled"),
    )
    assert run_during_sync(conn, {"feed_preview": {"enabled": False}}) is None


def test_run_during_sync_swallows_errors(conn, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("scrape exploded")

    monkeypatch.setattr(feed_preview, "cache_engaged_posts", _boom)
    messages: list[str] = []
    # Must not raise — a scrape failure cannot fail the sync.
    result = run_during_sync(conn, {}, on_message=messages.append)
    assert result is None
    assert messages and "skipped" in messages[0].lower()


def test_run_during_sync_runs_with_defaults(conn, monkeypatch) -> None:
    captured = {}

    def _fake(c, *, limit, sleep):
        captured["limit"] = limit
        captured["sleep"] = sleep
        return feed_preview.CacheStats(
            already_cached=0, skipped_unfetchable=0, to_fetch=1, fetched=1, ok=1, miss=0, err=0
        )

    monkeypatch.setattr(feed_preview, "cache_engaged_posts", _fake)
    messages: list[str] = []
    stats = run_during_sync(conn, {}, on_message=messages.append)
    assert stats is not None and stats["ok"] == 1
    assert captured == {"limit": 40, "sleep": 1.0}  # SYNC defaults
    assert any("Cached 1" in m for m in messages)

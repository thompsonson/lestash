"""Tests for MicroblogPublisher — slice 3 of Wave 2a.

Covers the behavioural §7.1 test list from
docs/ux-compose-and-categories-design.md:

1. publish() returns a PublishResult with non-empty URL.
2. publish() records a syndications row keyed by (item_id, target, target_url).
3. publish() twice raises AlreadyPublished unless caller opts in.
4. 5xx → PublishFailed; raw response attached.
5. 4xx → PublishRejected; message extracted from provider body.
6. lint() returns [] (Wave 3 ships the actual rules).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import Iterator
from unittest.mock import MagicMock

import httpx
import pytest
from lestash.plugins import (
    AlreadyPublished,
    ComposeRequest,
    Publisher,
    PublishFailed,
    PublishRejected,
    PublishResult,
)
from lestash_microblog.client import MicropubClient
from lestash_microblog.publisher import MicroblogPublisher

# --- fixtures ---


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the minimum schema MicroblogPublisher touches."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT,
            content TEXT NOT NULL
        );
        CREATE TABLE syndications (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            target_url TEXT NOT NULL,
            published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            request_body TEXT,
            response_body TEXT,
            UNIQUE(item_id, target, target_url)
        );
        INSERT INTO items (id, source_type, content) VALUES (42, 'note', 'x');
        """
    )
    yield conn
    conn.close()


@pytest.fixture
def get_db(db: sqlite3.Connection):
    """A context-manager factory that yields the shared connection."""

    @contextlib.contextmanager
    def _factory() -> Iterator[sqlite3.Connection]:
        yield db

    return _factory


@pytest.fixture
def fake_client() -> MagicMock:
    """Stand-in for MicropubClient. Tests set .create_entry.return_value."""
    return MagicMock(spec=MicropubClient)


@pytest.fixture
def publisher(fake_client: MagicMock, get_db) -> MicroblogPublisher:
    return MicroblogPublisher(client=fake_client, get_db=get_db)


@pytest.fixture
def compose() -> ComposeRequest:
    return ComposeRequest(
        item_id=42,
        title="A title",
        body="hello world",
        image_url=None,
        categories=("life",),
    )


def _http_error(status: int, body: dict | str) -> httpx.HTTPStatusError:
    """Build a synthetic HTTPStatusError with a controllable response."""
    if isinstance(body, dict):
        content = httpx.Response(status, json=body)
    else:
        content = httpx.Response(status, text=body)
    request = httpx.Request("POST", "https://micro.blog/micropub")
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=content)


# --- protocol conformance ---


def test_microblog_publisher_satisfies_protocol(publisher: MicroblogPublisher) -> None:
    assert isinstance(publisher, Publisher)


def test_target_is_classvar() -> None:
    assert MicroblogPublisher.target == "microblog"
    # ClassVar means the value lives on the class, not an instance attribute.
    assert "target" not in MicroblogPublisher.__init__.__code__.co_varnames


def test_lint_returns_empty_in_slice_3(
    publisher: MicroblogPublisher, compose: ComposeRequest
) -> None:
    # Wave 3 fills these in. For now the protocol is satisfied by [].
    assert publisher.lint(compose) == []


# --- happy path ---


def test_publish_success_returns_publish_result(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest
) -> None:
    fake_client.create_entry.return_value = (
        "https://matt.thompson.gr/2026/06/05/post.html",
        {"url": "https://matt.thompson.gr/2026/06/05/post.html"},
    )

    result = asyncio.run(publisher.publish(compose))

    assert isinstance(result, PublishResult)
    assert result.url.startswith("https://matt.thompson.gr/")
    assert result.target == "microblog"
    assert result.raw_response["url"].endswith("post.html")


def test_publish_passes_compose_fields_to_client(
    publisher: MicroblogPublisher, fake_client: MagicMock
) -> None:
    """Visibility maps to post-status; image_url to a single-element photo list."""
    fake_client.create_entry.return_value = ("u", {})
    compose = ComposeRequest(
        item_id=42,
        title="t",
        body="b",
        image_url="https://img.example/x.png",
        categories=("life", "reading"),
        visibility="draft",
    )

    asyncio.run(publisher.publish(compose))

    fake_client.create_entry.assert_called_once_with(
        content="b",
        name="t",
        categories=("life", "reading"),
        photo_urls=("https://img.example/x.png",),
        post_status="draft",
    )


def test_publish_omits_photo_when_no_image_url(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest
) -> None:
    fake_client.create_entry.return_value = ("u", {})

    asyncio.run(publisher.publish(compose))

    assert fake_client.create_entry.call_args.kwargs["photo_urls"] == ()


# --- idempotency / AlreadyPublished ---


def test_publish_raises_already_published_when_syndication_exists(
    publisher: MicroblogPublisher,
    fake_client: MagicMock,
    compose: ComposeRequest,
    db: sqlite3.Connection,
) -> None:
    db.execute(
        "INSERT INTO syndications (item_id, target, target_url) VALUES (?, ?, ?)",
        (compose.item_id, "microblog", "https://prior.example/post"),
    )

    with pytest.raises(AlreadyPublished) as exc:
        asyncio.run(publisher.publish(compose))

    assert "prior.example" in str(exc.value)
    fake_client.create_entry.assert_not_called()


def test_publish_bypasses_idempotency_when_flag_set(
    publisher: MicroblogPublisher,
    fake_client: MagicMock,
    compose: ComposeRequest,
    db: sqlite3.Connection,
) -> None:
    db.execute(
        "INSERT INTO syndications (item_id, target, target_url) VALUES (?, ?, ?)",
        (compose.item_id, "microblog", "https://prior.example/post"),
    )
    fake_client.create_entry.return_value = ("https://new.example/p", {})

    result = asyncio.run(publisher.publish(compose, if_not_already_published=True))

    assert result.url == "https://new.example/p"
    fake_client.create_entry.assert_called_once()


def test_publish_ignores_other_target_in_syndications(
    publisher: MicroblogPublisher,
    fake_client: MagicMock,
    compose: ComposeRequest,
    db: sqlite3.Connection,
) -> None:
    """A LinkedIn syndication must not block a microblog publish."""
    db.execute(
        "INSERT INTO syndications (item_id, target, target_url) VALUES (?, ?, ?)",
        (compose.item_id, "linkedin", "https://linkedin.com/post/123"),
    )
    fake_client.create_entry.return_value = ("https://new.example/p", {})

    result = asyncio.run(publisher.publish(compose))

    assert result.url == "https://new.example/p"


# --- error translation ---


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_4xx_raises_publish_rejected_with_provider_message(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest, status: int
) -> None:
    fake_client.create_entry.side_effect = _http_error(
        status, {"error": "invalid_request", "error_description": "missing scope"}
    )

    with pytest.raises(PublishRejected) as exc:
        asyncio.run(publisher.publish(compose))

    assert exc.value.message == "missing scope"
    assert exc.value.raw["error"] == "invalid_request"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_raises_publish_failed_with_raw_attached(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest, status: int
) -> None:
    fake_client.create_entry.side_effect = _http_error(status, {"error": "server_error"})

    with pytest.raises(PublishFailed) as exc:
        asyncio.run(publisher.publish(compose))

    assert exc.value.raw is not None
    assert exc.value.raw["error"] == "server_error"


def test_network_error_raises_publish_failed_with_no_raw(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest
) -> None:
    fake_client.create_entry.side_effect = httpx.ConnectError("DNS lookup failed")

    with pytest.raises(PublishFailed) as exc:
        asyncio.run(publisher.publish(compose))

    assert exc.value.raw is None
    assert "DNS" in exc.value.message


def test_4xx_with_non_json_body_falls_back_to_status_message(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest
) -> None:
    fake_client.create_entry.side_effect = _http_error(401, "plain text 401")

    with pytest.raises(PublishRejected) as exc:
        asyncio.run(publisher.publish(compose))

    assert exc.value.message == "HTTP 401"
    assert exc.value.raw["status_code"] == 401


def test_missing_location_raises_publish_failed(
    publisher: MicroblogPublisher, fake_client: MagicMock, compose: ComposeRequest
) -> None:
    """create_entry raises ValueError on 2xx-no-Location; treat as PublishFailed."""
    fake_client.create_entry.side_effect = ValueError(
        "Micropub server returned 201 but no Location header"
    )

    with pytest.raises(PublishFailed) as exc:
        asyncio.run(publisher.publish(compose))

    assert "Location" in exc.value.message

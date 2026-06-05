"""Tests for the POST /api/microblog/publish route (Wave 2a slice 3).

These exercise the route's translation layer:
- 201 on success, syndication row written.
- 409 on AlreadyPublished.
- 422 on PublishRejected with the provider's message in `detail`.
- 502 on PublishFailed.
- 401 when micro.blog isn't authenticated.

The MicroblogPublisher itself is covered by its own unit tests in
lestash-microblog/tests/test_publisher.py — this file patches the publisher
to focus on the HTTP-layer mapping.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from lestash.core.database import get_connection
from lestash.plugins import (
    AlreadyPublished,
    ComposeRequest,
    PublishFailed,
    PublishRejected,
    PublishResult,
)


@pytest.fixture
def seeded_item(test_config) -> int:
    """Insert one item for the publish payload to reference."""
    with get_connection(test_config) as conn:
        cursor = conn.execute(
            "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
            ("note", "compose-test", "body text"),
        )
        conn.commit()
        item_id = cursor.lastrowid
    assert item_id is not None
    return item_id


@pytest.fixture
def fake_publish_success() -> Iterator[MagicMock]:
    """Patch MicroblogPublisher.publish to return a canned PublishResult."""
    from lestash_microblog.publisher import MicroblogPublisher

    async def _ok(self, compose: ComposeRequest, **kwargs: Any) -> PublishResult:
        return PublishResult(
            url="https://matt.thompson.gr/2026/06/05/post.html",
            target="microblog",
            raw_response={"url": "https://matt.thompson.gr/2026/06/05/post.html"},
        )

    with patch.object(MicroblogPublisher, "publish", _ok) as p:
        yield p


@pytest.fixture
def fake_create_client() -> Iterator[MagicMock]:
    """Stub create_client so tests don't need a real micro.blog token file."""
    with patch("lestash_server.routes.microblog.create_client") as m:
        m.return_value = MagicMock()
        yield m


def _payload(item_id: int, **overrides: Any) -> dict:
    base = {
        "item_id": item_id,
        "title": "Hello",
        "body": "world",
        "image_url": None,
        "categories": ["life"],
        "visibility": "public",
    }
    base.update(overrides)
    return base


def test_publish_success_returns_201_and_url(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
    fake_publish_success: MagicMock,
    test_config,
) -> None:
    response = client.post("/api/microblog/publish", json=_payload(seeded_item))

    assert response.status_code == 201
    body = response.json()
    assert body["url"].endswith("post.html")
    assert body["target"] == "microblog"

    # Audit row exists.
    with get_connection(test_config) as conn:
        row = conn.execute(
            "SELECT target, target_url, request_body, response_body "
            "FROM syndications WHERE item_id = ?",
            (seeded_item,),
        ).fetchone()
    assert row is not None
    assert row["target"] == "microblog"
    assert row["target_url"].endswith("post.html")
    assert "world" in row["request_body"]
    assert "post.html" in row["response_body"]


def test_publish_already_published_returns_409(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
) -> None:
    from lestash_microblog.publisher import MicroblogPublisher

    async def _raise(self, compose: ComposeRequest, **kwargs: Any) -> PublishResult:
        raise AlreadyPublished(
            f"item {compose.item_id} already published to microblog: https://prior.example/p"
        )

    with patch.object(MicroblogPublisher, "publish", _raise):
        response = client.post("/api/microblog/publish", json=_payload(seeded_item))

    assert response.status_code == 409
    assert "already published" in response.json()["detail"]


def test_publish_4xx_from_provider_returns_422(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
) -> None:
    from lestash_microblog.publisher import MicroblogPublisher

    async def _rejected(self, compose: ComposeRequest, **kwargs: Any) -> PublishResult:
        raise PublishRejected("missing scope", {"error": "insufficient_scope"})

    with patch.object(MicroblogPublisher, "publish", _rejected):
        response = client.post("/api/microblog/publish", json=_payload(seeded_item))

    assert response.status_code == 422
    assert response.json()["detail"] == "missing scope"


def test_publish_5xx_from_provider_returns_502(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
) -> None:
    from lestash_microblog.publisher import MicroblogPublisher

    async def _failed(self, compose: ComposeRequest, **kwargs: Any) -> PublishResult:
        raise PublishFailed("server down", {"status_code": 503})

    with patch.object(MicroblogPublisher, "publish", _failed):
        response = client.post("/api/microblog/publish", json=_payload(seeded_item))

    assert response.status_code == 502
    assert response.json()["detail"] == "server down"


def test_publish_not_authenticated_returns_401(client: TestClient, seeded_item: int) -> None:
    """If create_client raises ValueError (no token), surface 401."""
    with patch("lestash_server.routes.microblog.create_client") as m:
        m.side_effect = ValueError("No token provided or found")

        response = client.post("/api/microblog/publish", json=_payload(seeded_item))

    assert response.status_code == 401
    assert "not authenticated" in response.json()["detail"]


def test_publish_forwards_if_not_already_published_flag(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
) -> None:
    """The flag must reach publisher.publish() as a kwarg."""
    from lestash_microblog.publisher import MicroblogPublisher

    captured: dict[str, Any] = {}

    async def _capture(self, compose: ComposeRequest, **kwargs: Any) -> PublishResult:
        captured.update(kwargs)
        return PublishResult(url="https://x.example/p", target="microblog", raw_response={})

    with patch.object(MicroblogPublisher, "publish", _capture):
        response = client.post(
            "/api/microblog/publish",
            json=_payload(seeded_item, if_not_already_published=True),
        )

    assert response.status_code == 201
    assert captured["if_not_already_published"] is True


def test_publish_rejects_unknown_visibility(
    client: TestClient,
    seeded_item: int,
    fake_create_client: MagicMock,
) -> None:
    """Pydantic enforces the Literal at the wire boundary."""
    response = client.post(
        "/api/microblog/publish",
        json=_payload(seeded_item, visibility="connections"),
    )
    assert response.status_code == 422

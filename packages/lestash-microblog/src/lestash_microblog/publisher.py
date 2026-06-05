"""MicroblogPublisher — the micro.blog implementation of the Publisher protocol.

Slice 3 of Wave 2a per docs/ux-compose-and-categories-design.md §7.2.

Responsibilities:

- Idempotency: before calling Micropub, check `syndications` for a prior
  successful publish of this item to this target. Raise `AlreadyPublished`
  unless the caller opted in via `if_not_already_published=True`.
- HTTP translation: wrap `MicropubClient.create_entry()` and turn
  `httpx.HTTPStatusError` / `httpx.RequestError` into the protocol's three
  exception classes per the §7.1 test list.
- Field mapping: `ComposeRequest.visibility` → Micropub `post-status`,
  `ComposeRequest.image_url` → photo list.

`lint()` returns `[]` here. The actual rule implementations (`YT_RAW_URL`,
`IMG_NOT_MARKDOWN`) ship in Wave 3 alongside the EmbedRenderer — that whole
slice is "insurance, not foundation" per the cadence section.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal

import httpx
from lestash.plugins import (
    AlreadyPublished,
    ComposeRequest,
    LintFinding,
    PublishFailed,
    PublishRejected,
    PublishResult,
)

from lestash_microblog.client import MicropubClient

# Type alias: a callable that returns a context-managed DB connection.
# The route injects `lestash_server.deps.get_db`; tests inject a fake.
DbContext = Callable[[], "contextlib.AbstractContextManager[sqlite3.Connection]"]


class MicroblogPublisher:
    """Publisher for micro.blog. Implements `lestash.plugins.Publisher`."""

    target: ClassVar[str] = "microblog"

    def __init__(self, client: MicropubClient, get_db: DbContext) -> None:
        self._client = client
        self._get_db = get_db

    def lint(self, compose: ComposeRequest) -> list[LintFinding]:
        """Lint placeholder. Wave 3 adds YT_RAW_URL + IMG_NOT_MARKDOWN."""
        return []

    async def publish(
        self,
        compose: ComposeRequest,
        *,
        if_not_already_published: bool = False,
    ) -> PublishResult:
        if not if_not_already_published:
            with self._get_db() as conn:
                row = conn.execute(
                    "SELECT target_url FROM syndications "
                    "WHERE item_id = ? AND target = ? "
                    "ORDER BY published_at DESC LIMIT 1",
                    (compose.item_id, self.target),
                ).fetchone()
                if row:
                    prior_url = row[0]
                    raise AlreadyPublished(
                        f"item {compose.item_id} already published to {self.target}: {prior_url}"
                    )

        # Annotate so the Literal narrows through asyncio.to_thread to
        # MicropubClient.create_entry's typed kwarg.
        post_status: Literal["published", "draft"] = (
            "draft" if compose.visibility == "draft" else "published"
        )
        photo_urls: tuple[str, ...] = (compose.image_url,) if compose.image_url else ()

        # MicropubClient is sync (httpx.Client). Bridge to the async route
        # via to_thread so we don't block the event loop on the HTTP call.
        try:
            url, raw = await asyncio.to_thread(
                self._client.create_entry,
                content=compose.body,
                name=compose.title,
                categories=compose.categories,
                photo_urls=photo_urls,
                post_status=post_status,
            )
        except httpx.HTTPStatusError as e:
            raw_body = _safe_json(e.response)
            status = e.response.status_code
            message = _extract_error_message(raw_body) or f"HTTP {status}"
            if 400 <= status < 500:
                raise PublishRejected(message, raw_body) from e
            raise PublishFailed(message, raw_body) from e
        except httpx.RequestError as e:
            # network / DNS / timeout — no response body to attach
            raise PublishFailed(str(e), None) from e
        except ValueError as e:
            # 2xx response with no Location header — server bug; treat as 5xx-like
            raise PublishFailed(str(e), None) from e

        return PublishResult(url=url, target=self.target, raw_response=raw)


def _safe_json(response: httpx.Response) -> Mapping[str, Any]:
    """Decode a response body as JSON, defaulting to a status-only dict."""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return {"status_code": response.status_code, "text": response.text[:500]}
    if isinstance(body, Mapping):
        return body
    return {"body": body}


def _extract_error_message(raw: Mapping[str, Any]) -> str | None:
    """Pull a human-readable message out of a Micropub error response.

    Micropub's spec uses `error_description`; some implementations use
    `message` or `error`. Return the first string we find.
    """
    for key in ("error_description", "message", "error"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None

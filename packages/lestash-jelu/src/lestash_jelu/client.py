"""Jelu REST API client (read-only).

Jelu (https://github.com/bayang/jelu) is a self-hosted reading tracker. It accepts
API tokens as ``Authorization: Bearer <token>``; create one in Jelu with the
``books:read``, ``reading:read``, ``reviews:read`` and ``lists:read`` scopes.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

PAGE_SIZE = 100


def get_config_path() -> Path:
    """Path of the saved Jelu URL and token."""
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "jelu.json"


def save_credentials(url: str, token: str) -> None:
    """Save the Jelu URL and API token (file readable by the owner only)."""
    path = get_config_path()
    path.write_text(json.dumps({"url": url.rstrip("/"), "token": token}, indent=2))
    os.chmod(path, 0o600)


def load_credentials() -> tuple[str, str] | None:
    """Load the Jelu URL and API token, or None if not configured."""
    path = get_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not data.get("url") or not data.get("token"):
        return None
    return data["url"], data["token"]


class JeluClient:
    """Minimal read-only client for Jelu's /api/v1."""

    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.url = url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.url}/api/v1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def __enter__(self) -> "JeluClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def _pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Iterate every element of a Spring Data paged endpoint."""
        page = 0
        while True:
            data = self._get(path, {**(params or {}), "page": page, "size": PAGE_SIZE})
            yield from data.get("content", [])
            total_pages = data.get("totalPages", 0)
            if data.get("last", True) or page + 1 >= total_pages:
                return
            page += 1

    def me(self) -> dict[str, Any]:
        """The user the token belongs to."""
        return self._get("/users/me")

    def userbooks(self) -> Iterator[dict[str, Any]]:
        """The user's books (with reading status, notes and the book itself)."""
        return self._pages("/userbooks")

    def reviews(self, user_id: str) -> Iterator[dict[str, Any]]:
        """Reviews written by the user."""
        return self._pages("/reviews", {"userId": user_id})

    def quotes(self, user_id: str) -> Iterator[dict[str, Any]]:
        """Quotes the user saved from books."""
        return self._pages("/book-quotes", {"userId": user_id})

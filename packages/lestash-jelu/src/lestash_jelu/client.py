"""Minimal read-only client for the Jelu REST API.

Jelu (https://github.com/bayang/jelu) is a self-hosted reading tracker. The client signs in
the way Jelu's own web UI does: ``GET /api/v1/users/me`` with HTTP Basic auth returns the
user and a session token, which later requests send as the ``X-Auth-Token`` header.

Jelu's scoped API tokens (``Authorization: Bearer``) are not used: Jelu only lets them reach
paths listed in its scope map, and ``/users/me`` and ``/book-quotes`` are not listed.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

PAGE_SIZE = 100
SESSION_HEADER = "X-Auth-Token"
# Jelu's default sorts (by dates) are not unique, so offset paging could skip or repeat rows
# when something changes mid-sync. Row ids are unique and never change.
STABLE_SORT = "id,asc"


class JeluAuthError(RuntimeError):
    """Jelu rejected the username/password."""


def get_config_path() -> Path:
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "jelu.json"


def save_credentials(url: str, username: str, password: str, token: str | None = None) -> None:
    """Save the login to jelu.json, readable only by you (mode 600)."""
    path = get_config_path()
    data = {"url": url.rstrip("/"), "username": username, "password": password}
    if token:
        data["token"] = token
    # Create with 600 from the start so the password is never world-readable
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.chmod(path, 0o600)


def load_credentials() -> dict[str, str] | None:
    """{"url", "username", "password", optional "token"}, or None if not configured."""
    path = get_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not all(data.get(k) for k in ("url", "username", "password")):
        return None
    return data


class JeluClient:
    """The few read-only Jelu calls the plugin needs, over a Jelu session."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._username = username
        self._password = password
        self.token = token
        self.user: dict[str, Any] | None = None
        self._client = httpx.Client(
            base_url=f"{self.url}/api/v1",
            headers={"Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def __enter__(self) -> "JeluClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def login(self) -> dict[str, Any]:
        """Sign in with Basic auth; keeps the session token and returns the user."""
        response = self._client.get("/users/me", auth=(self._username, self._password))
        if response.status_code == 401:
            raise JeluAuthError("Jelu rejected the username or password")
        response.raise_for_status()
        data = response.json()
        self.token = data.get("token") or response.headers.get(SESSION_HEADER)
        self.user = data["user"]
        return self.user

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            self.login()
        response = self._client.get(path, params=params, headers={SESSION_HEADER: self.token or ""})
        if response.status_code == 401:
            # The session expired (Jelu drops it after a week without use): sign in again once
            self.login()
            response = self._client.get(
                path, params=params, headers={SESSION_HEADER: self.token or ""}
            )
        response.raise_for_status()
        return response.json()

    def _pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            data = self._get(
                path, {**(params or {}), "page": page, "size": PAGE_SIZE, "sort": STABLE_SORT}
            )
            content = data.get("content", [])
            yield from content
            total_pages = data.get("totalPages")
            last = data.get("last")
            if last is None and total_pages is None:
                # Unknown page format: keep going until a page comes back empty
                if not content:
                    return
            elif last or page + 1 >= (total_pages or 0):
                return
            page += 1

    def me(self) -> dict[str, Any]:
        """The signed-in user ({"id", "login", ...}). Always checks the session with Jelu.

        ``/reviews`` answers anonymous callers with public reviews only, so an expired session
        must be caught here rather than silently returning less.
        """
        if not self.token:
            return self.login()
        data = self._get("/users/me")
        self.user = data["user"]
        return self.user

    def userbooks(self) -> Iterator[dict[str, Any]]:
        """Your books (Jelu defaults to the signed-in user)."""
        return self._pages("/userbooks")

    def reviews(self, user_id: str) -> Iterator[dict[str, Any]]:
        return self._pages("/reviews", {"userId": user_id})

    def quotes(self, user_id: str) -> Iterator[dict[str, Any]]:
        return self._pages("/book-quotes", {"userId": user_id})

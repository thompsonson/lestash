"""Audible API client wrapper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import audible
from lestash.core.config import get_config_dir
from lestash.core.logging import get_plugin_logger

logger = get_plugin_logger("audible")

LIBRARY_RESPONSE_GROUPS = "contributors,product_attrs,product_desc,product_details,media,series"

SIDECAR_URL = "https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar"


def get_auth_path() -> Path:
    """Get path to stored Audible auth file."""
    return get_config_dir() / "audible_auth.json"


def get_config_path() -> Path:
    """Get path to Audible plugin config."""
    return get_config_dir() / "audible_config.json"


def load_config() -> dict[str, Any]:
    """Load plugin config (marketplace, etc.)."""
    path = get_config_path()
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_config(config: dict[str, Any]) -> None:
    """Save plugin config."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def is_authenticated() -> bool:
    """Check if we have stored auth credentials."""
    return get_auth_path().exists()


def build_login_url(locale: str = "us") -> dict[str, Any]:
    """Build the Amazon OAuth URL for Audible authentication.

    Returns a dict with the login URL and state needed to complete auth.
    The user visits the URL, logs in, then provides the redirect URL back.

    Args:
        locale: Audible marketplace code.

    Returns:
        Dict with keys: url, code_verifier, serial, locale, domain.
    """
    from audible.localization import Locale
    from audible.login import build_oauth_url as _build_oauth_url
    from audible.login import create_code_verifier

    loc = Locale(locale)
    code_verifier = create_code_verifier()
    oauth_url, serial = _build_oauth_url(
        country_code=loc.country_code,
        domain=loc.domain,
        market_place_id=loc.market_place_id,
        code_verifier=code_verifier,
        serial=None,
    )
    return {
        "url": oauth_url,
        "code_verifier": code_verifier.decode(),
        "serial": serial,
        "locale": locale,
        "domain": loc.domain,
    }


def complete_auth(redirect_url: str, state: dict[str, Any]) -> audible.Authenticator:
    """Complete authentication using the redirect URL from Amazon.

    Args:
        redirect_url: The URL from the browser after Amazon login
            (contains openid.oa2.authorization_code parameter).
        state: The state dict returned by build_login_url().

    Returns:
        Authenticated Authenticator instance.
    """
    from urllib.parse import parse_qs

    import httpx
    from audible.register import register as register_device

    # Extract authorization code from redirect URL
    parsed = httpx.URL(redirect_url)
    params = parse_qs(parsed.query.decode())
    if "openid.oa2.authorization_code" not in params:
        raise ValueError(
            "No authorization code found in URL. "
            "Make sure you copied the full URL after signing in."
        )
    auth_code = params["openid.oa2.authorization_code"][0]

    # Register device to get tokens
    register_data = register_device(
        authorization_code=auth_code,
        code_verifier=state["code_verifier"].encode(),
        domain=state["domain"],
        serial=state["serial"],
    )

    # Build Authenticator from registration data
    locale = state["locale"]
    auth = audible.Authenticator()
    auth.locale = locale  # type: ignore[assignment]
    auth._update_attrs(**register_data)  # noqa: SLF001

    # Save credentials
    auth_path = get_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth.to_file(str(auth_path))
    save_config({"locale": locale})
    logger.info("Authentication successful, credentials saved")
    return auth


def load_auth() -> audible.Authenticator | None:
    """Load stored auth credentials."""
    auth_path = get_auth_path()
    if not auth_path.exists():
        return None
    try:
        return audible.Authenticator.from_file(str(auth_path))
    except Exception:
        logger.warning("Failed to load auth credentials", exc_info=True)
        return None


def get_client() -> audible.Client | None:
    """Get an authenticated Audible API client."""
    auth = load_auth()
    if not auth:
        return None
    config = load_config()
    locale = config.get("locale", "us")
    return audible.Client(auth=auth, country_code=locale)


def get_library(client: audible.Client) -> list[dict[str, Any]]:
    """Fetch user's audiobook library.

    Returns:
        List of book dicts with title, author, ASIN, etc.
    """
    books: list[dict[str, Any]] = []
    page = 1
    page_size = 50

    while True:
        response = client.get(
            "1.0/library",
            params={
                "num_results": page_size,
                "page": page,
                "response_groups": LIBRARY_RESPONSE_GROUPS,
            },
        )
        items = response.get("items", [])
        if not items:
            break
        books.extend(items)
        if len(items) < page_size:
            break
        page += 1

    logger.info(f"Fetched {len(books)} books from library")
    return books


def get_bookmarks(client: audible.Client, asin: str) -> dict[str, Any]:
    """Fetch bookmarks, clips, and notes for a specific book.

    Args:
        client: Authenticated Audible client.
        asin: Book ASIN.

    Returns:
        Dict with bookmark data from the sidecar endpoint.
    """
    response = client.get(
        SIDECAR_URL,
        params={"type": "AUDI", "key": asin},
    )
    return response


def extract_book_metadata(book: dict[str, Any]) -> dict[str, Any]:
    """Extract useful metadata from a library book entry."""
    authors = []
    narrators = []
    for contributor in book.get("authors", []):
        authors.append(contributor.get("name", ""))
    for contributor in book.get("narrators", []):
        narrators.append(contributor.get("name", ""))

    return {
        "asin": book.get("asin", ""),
        "title": book.get("title", ""),
        "subtitle": book.get("subtitle"),
        "authors": authors,
        "narrators": narrators,
        "runtime_minutes": book.get("runtime_length_min"),
        "release_date": book.get("release_date"),
        "publisher": book.get("publisher_name"),
        "language": book.get("language"),
        "cover_url": book.get("product_images", {}).get("500"),
        "series": [
            {"name": s.get("title"), "position": s.get("sequence")} for s in book.get("series", [])
        ],
        "format_type": book.get("format_type"),
        "content_type": book.get("content_type"),
    }

"""Audible API client wrapper.

Authentication uses Android device parameters (matching Libation) instead of
the audible library's broken iOS flow. Only the OAuth URL building and device
registration are custom; everything else (Locale, PKCE, Authenticator, Client)
reuses the audible library.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

import audible
import httpx
from lestash.core.config import get_config_dir
from lestash.core.logging import get_plugin_logger

logger = get_plugin_logger("audible")

LIBRARY_RESPONSE_GROUPS = "contributors,product_attrs,product_desc,product_details,media,series"
SIDECAR_URL = "https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar"

# Android device constants (from Libation's AudibleApi)
DEVICE_TYPE = "A2CZJZGLK2JJVM"
APP_VERSION = "3.84.0"
SOFTWARE_VERSION = "2090254511"
OS_VERSION = "13"


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


def _build_device_serial() -> str:
    """Generate a 20-byte random device serial (matching Libation's format)."""
    return secrets.token_hex(20)


def _build_client_id(serial: str) -> str:
    """Build client ID from serial + device type."""
    return (serial.encode() + f"#{DEVICE_TYPE}".encode()).hex()


def build_login_url(locale: str = "us") -> dict[str, Any]:
    """Build the Amazon OAuth URL using Android device parameters.

    Uses the same OAuth flow as Libation (Android Audible app identity)
    instead of the audible library's broken iOS flow.

    Args:
        locale: Audible marketplace code.

    Returns:
        Dict with keys: url, code_verifier, serial, locale, domain.
    """
    from audible.localization import Locale
    from audible.login import create_code_verifier, create_s256_code_challenge

    loc = Locale(locale)
    code_verifier = create_code_verifier()
    code_challenge = create_s256_code_challenge(code_verifier)
    serial = _build_device_serial()
    client_id = _build_client_id(serial)
    cc = loc.country_code

    base_url = f"https://www.amazon.{loc.domain}/ap/signin"
    return_to = f"https://www.amazon.{loc.domain}/ap/maplanding"

    oauth_params = {
        "openid.oa2.response_type": "code",
        "openid.oa2.code_challenge_method": "S256",
        "openid.oa2.code_challenge": code_challenge,
        "openid.return_to": return_to,
        "openid.assoc_handle": f"amzn_audible_android_aui_{cc}",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "pageId": f"amzn_audible_android_aui_v2_dark_{cc.upper()}",
        "accountStatusPolicy": "P1",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.mode": "checkid_setup",
        "openid.ns.oa2": "http://www.amazon.com/ap/ext/oauth/2",
        "openid.oa2.client_id": f"device:{client_id}",
        "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
        "marketPlaceId": loc.market_place_id,
        "openid.oa2.scope": "device_auth_access",
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.pape.max_auth_age": "0",
        "disableLoginPrepopulate": "1",
    }

    url = f"{base_url}?{urlencode(oauth_params)}"
    return {
        "url": url,
        "code_verifier": code_verifier.decode(),
        "serial": serial,
        "locale": locale,
        "domain": loc.domain,
    }


def complete_auth(redirect_url: str, state: dict[str, Any]) -> audible.Authenticator:
    """Complete authentication using the redirect URL from Amazon.

    Registers an Android device (matching Libation) and stores the tokens.

    Args:
        redirect_url: The URL from the browser after Amazon login
            (contains openid.oa2.authorization_code parameter).
        state: The state dict returned by build_login_url().

    Returns:
        Authenticated Authenticator instance.
    """
    # Extract authorization code from redirect URL
    parsed = httpx.URL(redirect_url)
    params = parse_qs(parsed.query.decode())
    if "openid.oa2.authorization_code" not in params:
        raise ValueError(
            "No authorization code found in URL. "
            "Make sure you copied the full URL after signing in."
        )
    auth_code = params["openid.oa2.authorization_code"][0]

    serial = state["serial"]
    domain = state["domain"]
    locale = state["locale"]
    client_id = _build_client_id(serial)

    # Register Android device (matching Libation's registration body)
    body = {
        "requested_token_type": [
            "bearer",
            "mac_dms",
            "website_cookies",
            "store_authentication_cookie",
        ],
        "cookies": {"website_cookies": [], "domain": f".amazon.{domain}"},
        "registration_data": {
            "domain": "Device",
            "app_version": APP_VERSION,
            "device_serial": serial,
            "device_type": DEVICE_TYPE,
            "device_name": (
                "%FIRST_NAME%%FIRST_NAME_POSSESSIVE_STRING%%DUPE_STRATEGY_1ST%Audible for Android"
            ),
            "os_version": OS_VERSION,
            "software_version": SOFTWARE_VERSION,
            "device_model": "Android",
            "app_name": "Audible",
        },
        "auth_data": {
            "client_id": client_id,
            "authorization_code": auth_code,
            "code_verifier": state["code_verifier"],
            "code_algorithm": "SHA-256",
            "client_domain": "DeviceLegacy",
        },
        "requested_extensions": ["device_info", "customer_info"],
    }

    resp = httpx.post(f"https://api.amazon.{domain}/auth/register", json=body)
    resp_json = resp.json()
    if resp.status_code != 200:
        raise ValueError(f"Device registration failed: {resp_json}")

    # Parse tokens from response
    success = resp_json["response"]["success"]
    tokens = success["tokens"]
    extensions = success["extensions"]
    expires_s = int(tokens["bearer"]["expires_in"])

    register_data = {
        "adp_token": tokens["mac_dms"]["adp_token"],
        "device_private_key": tokens["mac_dms"]["device_private_key"],
        "access_token": tokens["bearer"]["access_token"],
        "refresh_token": tokens["bearer"]["refresh_token"],
        "expires": (datetime.now(UTC) + timedelta(seconds=expires_s)).timestamp(),
        "website_cookies": {
            c["Name"]: c["Value"].replace('"', "") for c in tokens["website_cookies"]
        },
        "store_authentication_cookie": tokens["store_authentication_cookie"],
        "device_info": extensions["device_info"],
        "customer_info": extensions["customer_info"],
    }

    # Build Authenticator from registration data
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


def get_bookmarks(client: audible.Client, asin: str) -> list[dict[str, Any]]:
    """Fetch bookmarks, clips, and notes for a specific book.

    Args:
        client: Authenticated Audible client.
        asin: Book ASIN.

    Returns:
        List of bookmark/note records from the sidecar endpoint.
    """
    try:
        response = client.get(
            SIDECAR_URL,
            params={"type": "AUDI", "key": asin},
        )
    except Exception:
        return []

    # Records are nested under payload.records
    records = response.get("payload", {}).get("records", [])
    if not isinstance(records, list):
        return []
    return records


def extract_book_metadata(book: dict[str, Any]) -> dict[str, Any]:
    """Extract useful metadata from a library book entry."""
    authors = []
    narrators = []
    for contributor in book.get("authors") or []:
        authors.append(contributor.get("name", ""))
    for contributor in book.get("narrators") or []:
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
            {"name": s.get("title"), "position": s.get("sequence")}
            for s in (book.get("series") or [])
        ],
        "format_type": book.get("format_type"),
        "content_type": book.get("content_type"),
    }

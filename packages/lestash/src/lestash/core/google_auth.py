"""Shared Google OAuth 2.0 module for Drive, Docs, and other Google APIs.

Supports headless (SSH) environments via console-based auth flow.
Credentials are shared across all Google integrations.
"""

import json
import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Default scopes — callers can request additional scopes
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
]

CREDENTIALS_FILE = "google_credentials.json"
CLIENT_SECRETS_FILE = "google_client_secrets.json"


def get_config_dir() -> Path:
    """Get lestash config directory."""
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_client_secrets_path() -> Path:
    return get_config_dir() / CLIENT_SECRETS_FILE


def get_credentials_path() -> Path:
    return get_config_dir() / CREDENTIALS_FILE


def is_headless() -> bool:
    """Detect if running in a headless/SSH environment."""
    return bool(os.environ.get("SSH_TTY") or os.environ.get("SSH_CLIENT"))


def save_credentials(credentials: Credentials, scopes: list[str] | None = None) -> None:
    """Save OAuth credentials to config file."""
    creds_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else (scopes or DEFAULT_SCOPES),
    }
    path = get_credentials_path()
    path.write_text(json.dumps(creds_data, indent=2))
    path.chmod(0o600)


def load_credentials() -> Credentials | None:
    """Load OAuth credentials from config file."""
    path = get_credentials_path()
    if not path.exists():
        return None

    try:
        creds_data = json.loads(path.read_text())
        return Credentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", DEFAULT_SCOPES),
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def run_auth_flow(
    scopes: list[str] | None = None,
    headless: bool | None = None,
) -> Credentials:
    """Run OAuth 2.0 flow to get user credentials.

    Args:
        scopes: OAuth scopes to request. Defaults to DEFAULT_SCOPES.
        headless: Force headless (console) mode. Auto-detected if None.

    Returns:
        Authenticated credentials.
    """
    secrets_path = get_client_secrets_path()
    if not secrets_path.exists():
        msg = (
            f"Client secrets file not found at {secrets_path}.\n"
            "Download it from Google Cloud Console:\n"
            "1. Go to https://console.cloud.google.com/apis/credentials\n"
            "2. Create OAuth 2.0 Client ID (Desktop application)\n"
            "3. Download the JSON and save it as:\n"
            f"   {secrets_path}"
        )
        raise FileNotFoundError(msg)

    effective_scopes = scopes or DEFAULT_SCOPES
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), effective_scopes)

    use_headless = headless if headless is not None else is_headless()
    credentials = _run_manual_flow(flow) if use_headless else flow.run_local_server(port=0)

    save_credentials(credentials, effective_scopes)
    return credentials


def _run_manual_flow(flow: InstalledAppFlow) -> Credentials:
    """Manual OAuth flow for headless/SSH environments.

    Prints a URL for the user to visit, then accepts the authorization code.
    """
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")

    print(f"Visit this URL to authorize:\n\n  {auth_url}\n")
    code = input("Enter the authorization code: ").strip()

    flow.fetch_token(code=code)
    return flow.credentials


def get_credentials(scopes: list[str] | None = None) -> Credentials:
    """Get valid OAuth credentials, refreshing if necessary.

    Returns:
        Valid credentials.

    Raises:
        ValueError: If no credentials available.
    """
    credentials = load_credentials()

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            save_credentials(credentials, scopes)
            return credentials
        except Exception:
            pass

    raise ValueError("No valid Google credentials. Run 'lestash google auth' to authenticate.")


def check_auth_status() -> dict:
    """Check Google auth status. Returns a status dict."""
    result: dict[str, object] = {
        "client_secrets_exists": get_client_secrets_path().exists(),
        "credentials_exists": get_credentials_path().exists(),
        "authenticated": False,
        "scopes": [],
    }

    credentials = load_credentials()
    if credentials:
        result["scopes"] = list(credentials.scopes) if credentials.scopes else []
        if credentials.valid:
            result["authenticated"] = True
        elif credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                save_credentials(credentials)
                result["authenticated"] = True
            except Exception as e:
                result["refresh_error"] = str(e)

    return result


def extract_drive_file_id(url_or_id: str) -> str:
    """Extract Google Drive file ID from a URL or return as-is if already an ID."""
    # Match /d/<id>/ or /file/d/<id>/
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Match id= parameter
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)
    # Assume it's already a file ID
    return url_or_id


def download_drive_file(file_id: str, output_dir: Path | None = None) -> Path:
    """Download a file from Google Drive.

    Args:
        file_id: Google Drive file ID.
        output_dir: Directory to save the file. Defaults to cache dir.

    Returns:
        Path to the downloaded file.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload

    credentials = get_credentials()
    service = build("drive", "v3", credentials=credentials)

    # Get file metadata
    file_meta = service.files().get(fileId=file_id, fields="name,size,mimeType").execute()
    file_name = file_meta["name"]

    # Prepare output path
    if output_dir is None:
        output_dir = get_config_dir() / "cache"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / file_name

    # Download
    request = service.files().get_media(fileId=file_id)
    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return output_path

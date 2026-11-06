"""Gmail access through the Google Workspace CLI (`gws`), read-only.

`gws` holds its own OAuth credentials (sign in once with
`gws auth login --scopes https://www.googleapis.com/auth/gmail.readonly`), so this
plugin never sees or stores a Google token.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# systemd user services don't have Homebrew on PATH, so also look in the usual places.
# Gmail limits API "units per minute per user"; on hitting it, wait for the next minute.
RATE_LIMIT_MARKERS = ("Quota exceeded", "rateLimitExceeded", "Rate Limit Exceeded", "429")
RETRY_DELAYS = (15, 30, 60, 60)

GWS_FALLBACKS = [
    "/home/linuxbrew/.linuxbrew/bin/gws",
    "/opt/homebrew/bin/gws",
    "/usr/local/bin/gws",
    str(Path.home() / ".local" / "bin" / "gws"),
]


class GwsError(RuntimeError):
    """`gws` failed or returned something that isn't JSON."""


def find_gws(configured: str | None = None) -> str | None:
    """Path to the gws binary: configured, on PATH, or a known install location."""
    for candidate in [configured, shutil.which("gws"), *GWS_FALLBACKS]:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def get_config_path() -> Path:
    config_dir = Path.home() / ".config" / "lestash"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "gmail.json"


def load_config() -> dict[str, Any]:
    """Plugin settings: {"queries": [...], "gws_path": optional}."""
    path = get_config_path()
    if not path.exists():
        return {"queries": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"queries": []}
    data.setdefault("queries", [])
    return data


def save_config(config: dict[str, Any]) -> None:
    get_config_path().write_text(json.dumps(config, indent=2))


class GwsGmail:
    """The few Gmail calls the plugin needs, run through `gws`."""

    def __init__(self, gws_path: str, runner=subprocess.run, sleep=time.sleep) -> None:
        self.gws_path = gws_path
        self._run = runner
        self._sleep = sleep

    def _call(self, *args: str, params: dict[str, Any]) -> Any:
        """Run one call, waiting and retrying when Gmail's per-minute quota is hit."""
        for delay in RETRY_DELAYS:
            try:
                return self._call_once(*args, params=params)
            except GwsError as e:
                if not any(marker in str(e) for marker in RATE_LIMIT_MARKERS):
                    raise
                self._sleep(delay)
        return self._call_once(*args, params=params)

    def _call_once(self, *args: str, params: dict[str, Any]) -> Any:
        proc = self._run(
            [self.gws_path, "gmail", "users", *args, "--params", json.dumps(params)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise GwsError(f"gws {' '.join(args)} failed (exit {proc.returncode}): {detail}") from e
        if isinstance(data, dict) and "error" in data:
            raise GwsError(f"gws {' '.join(args)}: {data['error'].get('message', data['error'])}")
        return data

    def profile(self) -> dict[str, Any]:
        return self._call("getProfile", params={"userId": "me"})

    def thread_ids(self, query: str, limit: int | None = None) -> Iterator[str]:
        """Thread ids matching a Gmail search, newest first, without duplicates."""
        seen: set[str] = set()
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            data = self._call("messages", "list", params=params)
            for message in data.get("messages", []):
                thread_id = message["threadId"]
                if thread_id not in seen:
                    seen.add(thread_id)
                    yield thread_id
                    if limit and len(seen) >= limit:
                        return
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def thread(self, thread_id: str) -> dict[str, Any]:
        return self._call(
            "threads", "get", params={"userId": "me", "id": thread_id, "format": "full"}
        )

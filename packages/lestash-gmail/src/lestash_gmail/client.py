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

# Gmail limits API "units per minute per user"; on hitting it, wait for the next minute.
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
RATE_LIMIT_TEXT = ("Quota exceeded", "rateLimitExceeded", "userRateLimitExceeded")
RETRY_DELAYS = (15, 30, 60, 60)
CALL_TIMEOUT = 120  # seconds per gws call

# systemd user services don't have Homebrew on PATH, so also look in the usual places.

GWS_FALLBACKS = [
    "/home/linuxbrew/.linuxbrew/bin/gws",
    "/opt/homebrew/bin/gws",
    "/usr/local/bin/gws",
    str(Path.home() / ".local" / "bin" / "gws"),
]


class GwsError(RuntimeError):
    """`gws` failed, timed out, or returned something that isn't JSON.

    ``code`` and ``reason`` come from the Gmail API error when gws returned one.
    """

    def __init__(self, message: str, *, code: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.reason = reason

    @property
    def is_rate_limit(self) -> bool:
        """Gmail's per-user quota was hit (worth waiting and retrying)."""
        if self.code == 429 or self.reason in RATE_LIMIT_REASONS:
            return True
        if self.code is not None:
            # A structured error that isn't a quota error, e.g. 404 or 400
            return self.code == 403 and "quota" in str(self).lower()
        # gws failed before returning JSON: look for the quota wording only
        return any(marker in str(self) for marker in RATE_LIMIT_TEXT)


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
                if not e.is_rate_limit:
                    raise
                self._sleep(delay)
        return self._call_once(*args, params=params)

    def _call_once(self, *args: str, params: dict[str, Any]) -> Any:
        call = " ".join(args)
        try:
            proc = self._run(
                [self.gws_path, "gmail", "users", *args, "--params", json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=CALL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise GwsError(f"gws {call} timed out after {CALL_TIMEOUT}s") from e
        except OSError as e:
            raise GwsError(f"gws {call} could not run: {e}") from e
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as e:
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise GwsError(f"gws {call} failed (exit {proc.returncode}): {detail}") from e
        if isinstance(data, dict) and "error" in data:
            error = data["error"] if isinstance(data["error"], dict) else {"message": data["error"]}
            reason = error.get("reason") or next(
                (e.get("reason") for e in error.get("errors") or [] if e.get("reason")), None
            )
            code = error.get("code")
            raise GwsError(
                f"gws {call}: {error.get('message', error)}",
                code=code if isinstance(code, int) else None,
                reason=reason,
            )
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

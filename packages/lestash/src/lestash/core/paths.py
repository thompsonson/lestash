"""Where LeStash keeps its files: the one module that names its folders (#304).

LeStash follows the XDG layout. Each folder honours its ``XDG_*`` variable:

| Folder | Variable | Holds | Backed up |
|---|---|---|---|
| ``~/.config/lestash`` | ``XDG_CONFIG_HOME`` | config.toml, secrets, switch files | yes |
| ``~/.local/share/lestash`` | ``XDG_DATA_HOME`` | database, file store, Kobo, media | yes |
| ``~/.local/share/lestash/backups`` | | LeStash's own database copies | no |
| ``~/.cache/lestash`` | ``XDG_CACHE_HOME`` | Drive downloads, voice, models | no |
| ``~/.local/state/lestash`` | ``XDG_STATE_HOME`` | logs, sync locks, Bluesky session | no |

Before #304 everything lived in ``~/.config/lestash``, the *legacy* folder. Until the
one-time move, a file still in the legacy folder is used in place of the new one
(``_either``). Every lookup is made at call time, so it follows the home folder and the
variables of the moment, as the tests need.

These are the defaults, the last step of one precedence, after the Twelve-Factor App
(https://12factor.net): III, config in the environment, and IV, a backing service named
by config alone. A setting comes from its environment variable first, then config.toml,
then this module (``lestash.core.config.ENV_OVERRIDES``). A ``database_path`` other than
the default keeps its media, store, post cache, locks and backups beside that database
(``lestash.core.config.custom_db_dir``).
"""

from __future__ import annotations

import os
from pathlib import Path

APP = "lestash"
DB_NAME = "lestash.db"


def _base(variable: str, default: str) -> Path:
    # The XDG spec says to ignore a relative value
    value = os.environ.get(variable, "")
    return Path(value) if value and Path(value).is_absolute() else Path.home() / default


def legacy_dir() -> Path:
    """``~/.config/lestash``, where every file lived before #304."""
    return Path.home() / ".config" / APP


def config_dir() -> Path:
    """Settings, secrets and switch files."""
    return _base("XDG_CONFIG_HOME", ".config") / APP


def data_dir() -> Path:
    """Data that can't be made again: the database and the stores."""
    return _base("XDG_DATA_HOME", ".local/share") / APP


def cache_dir() -> Path:
    """Files that can be downloaded or made again."""
    return _base("XDG_CACHE_HOME", ".cache") / APP


def state_dir() -> Path:
    """Logs, locks and sessions: kept between runs, not backed up."""
    return _base("XDG_STATE_HOME", ".local/state") / APP


def _either(new: Path, legacy: Path) -> Path:
    """``legacy`` while it is there, else ``new``.

    The legacy path wins even when both exist: until the move it holds the real file,
    and a file at the new path may be an empty leftover. pop-mini has a 0-byte
    ``~/.local/share/lestash/lestash.db`` beside the real database in the legacy folder.
    """
    return legacy if legacy.exists() else new


def settings_dir() -> Path:
    """The folder of ``config.toml`` and the secrets."""
    return _either(config_dir(), legacy_dir())


def database_path() -> Path:
    """The default database."""
    return _either(data_dir() / DB_NAME, legacy_dir() / DB_NAME)


def store_dir() -> Path:
    """The default file store's root (``lestash.core.blobs``)."""
    return data_dir()


def kobo_dir() -> Path:
    """The Kobo snapshot store (``lestash_kobo.store``)."""
    return data_dir() / "kobo"


def media_dir() -> Path:
    """The default media folder: item attachments not yet moved into the store."""
    return _either(data_dir() / "media", legacy_dir() / "media")


def backups_dir() -> Path:
    """LeStash's own copies of the default database. Not backed up."""
    return data_dir() / "backups"


def logs_dir() -> Path:
    return _either(state_dir() / "logs", legacy_dir() / "logs")


def locks_dir() -> Path:
    """Sync lock files for the default database."""
    return state_dir() / "locks"


def bluesky_session_path() -> Path:
    return _either(state_dir() / "bluesky_session.json", legacy_dir() / "bluesky_session.json")


def downloads_dir() -> Path:
    """Files downloaded from Drive by ``lestash google download`` and the import route.

    Takeout zips are among them. They stay on Drive, so they are cache.
    """
    return cache_dir() / "downloads"


def drive_cache_dir() -> Path:
    """Drive files read for text extraction and imports."""
    return _either(cache_dir() / "drive", legacy_dir() / "cache" / "drive")


def voice_cache_dir() -> Path:
    return _either(cache_dir() / "voice", legacy_dir() / "cache" / "voice")


def posts_cache_dir() -> Path:
    """LinkedIn post screenshots for the default database."""
    return _either(cache_dir() / "posts", legacy_dir() / "cache" / "posts")


def whisper_models_dir() -> Path:
    return _either(cache_dir() / "whisper-models", data_dir() / "whisper-models")


def speaker_models_dir() -> Path:
    return _either(cache_dir() / "speaker-models", data_dir() / "speaker-models")


def switch_file(name: str) -> Path:
    """A file whose presence turns something off, such as ``kobo-no-eject``.

    ``deploy/lestash-kobo.service`` checks the same folder: config_dir().
    """
    return settings_dir() / name

"""Configuration management for Le Stash."""

import copy
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict


def get_config_dir() -> Path:
    """Get the configuration directory path."""
    config_dir = Path.home() / ".config" / "lestash"
    return config_dir


def get_config_path() -> Path:
    """Get the configuration file path."""
    return get_config_dir() / "config.toml"


def get_database_path() -> Path:
    """Get the default database path."""
    return get_config_dir() / "lestash.db"


def get_log_path() -> Path:
    """Get the default log file path."""
    return get_config_dir() / "logs" / "lestash.log"


class GeneralConfig(BaseModel):
    """General configuration."""

    database_path: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.database_path:
            self.database_path = str(get_database_path())


class LoggingConfig(BaseModel):
    """Logging configuration."""

    # Global log level
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Console settings (diagnostic output to stderr)
    console_enabled: bool = True
    console_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    console_timestamps: bool = False

    # File settings (persistent debug trail)
    file_enabled: bool = True
    file_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "DEBUG"
    file_path: str = ""
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    file_format: Literal["simple", "detailed", "json"] = "detailed"

    # Database settings (optional, queryable history)
    db_enabled: bool = False
    db_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Filter noisy third-party loggers
    filters: dict[str, str] = {
        "httpx": "WARNING",
        "httpcore": "WARNING",
        "urllib3": "WARNING",
    }

    def model_post_init(self, __context: Any) -> None:
        if not self.file_path:
            self.file_path = str(get_log_path())


def _replace_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` all at once, never leaving a half-written file.

    A symlinked config stays a symlink (its target is replaced), and the file keeps its
    permissions. A new file gets the umask's usual mode, as open() would give it.
    """
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # on disk before the rename, so a crash can't empty it
        if target.exists():
            mode = stat.S_IMODE(target.stat().st_mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def as_list(value: object) -> list[Any]:
    """Read a list setting. One value given as text is a one-item list, not its letters."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return list(value)
    return [value]


def as_bool(value: object) -> bool:
    """Read a config switch. Older ``lestash config set`` stored strings, so "false" is False."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class Config(BaseModel):
    """Application configuration.

    Besides ``[general]`` and ``[logging]``, each plugin reads its own table, e.g.
    ``[kobo]``; those are kept as they are on load and save (#208).
    """

    model_config = ConfigDict(extra="allow")

    general: GeneralConfig = GeneralConfig()
    logging: LoggingConfig = LoggingConfig()

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from file."""
        config_path = get_config_path()
        if config_path.exists():
            with config_path.open("rb") as f:
                return cls(**tomllib.load(f))
        return cls()

    def save(self) -> None:
        """Save configuration to file, only if ``load`` reads it back the same.

        Written with tomli-w and read with tomllib, both TOML 1.0, so text with
        backslashes and lists mixing kinds survive the round trip.

        Raises:
            ValueError: the config can't be written as TOML that ``load`` reads back
                unchanged. The file is left as it was.
        """
        data = self.model_dump()
        try:
            text = tomli_w.dumps(data)
            reloaded = tomli_w.dumps(type(self)(**tomllib.loads(text)).model_dump())
        except Exception as e:
            raise ValueError(f"not saved: the config would not load again ({e!r})") from e
        # Compared as text, so a nan setting (nan != nan) doesn't block every save
        if reloaded != text:
            raise ValueError("not saved: the config would not load back the same")
        _replace_file(get_config_path(), text)

    def get_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """The plugin's ``[plugin_name]`` table, or ``{}`` when it has none.

        A copy, so a plugin changing it can't change the config.
        """
        section = (self.model_extra or {}).get(plugin_name)
        return copy.deepcopy(section) if isinstance(section, dict) else {}


def init_config() -> Config:
    """Initialize configuration with defaults."""
    config = Config()
    config.save()
    return config

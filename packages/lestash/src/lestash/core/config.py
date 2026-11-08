"""Configuration management for Le Stash."""

import copy
from pathlib import Path
from typing import Any, Literal

import toml
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
            data = toml.load(config_path)
            return cls(**data)
        return cls()

    def save(self) -> None:
        """Save configuration to file."""
        config_path = get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            toml.dump(self.model_dump(), f)

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

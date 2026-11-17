"""Configuration management for Le Stash."""

import copy
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from lestash.core import paths


def get_config_dir() -> Path:
    """The folder of ``config.toml`` and the credential files (``lestash.core.paths``)."""
    return paths.settings_dir()


def secrets_dir() -> Path:
    """The config folder, made readable only by you if it is made here (#189)."""
    config_dir = get_config_dir()
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return config_dir


def get_config_path() -> Path:
    """Get the configuration file path."""
    return get_config_dir() / "config.toml"


def get_database_path() -> Path:
    """The default database: ``~/.local/share/lestash/lestash.db``, or the legacy one."""
    return paths.database_path()


def get_log_path() -> Path:
    """The default log file: ``~/.local/state/lestash/logs/``, or the legacy one."""
    return paths.logs_dir() / "lestash.log"


class GeneralConfig(BaseModel):
    """General configuration."""

    database_path: str = ""
    # The file store's folder; empty: see lestash.core.blobs.store_root
    store_path: str = ""

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


def write_secret(path: Path, text: str) -> None:
    """Write a credential or token file readable only by you (mode 600), all at once.

    The file is made at 600 by mkstemp, so the secret is never in a readable file, and
    swapped in whole, so a crash can't leave it empty. Its folder, if made here, is 700.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
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


# Environment variables that override a config.toml setting, after Twelve-Factor III
# (config in the environment) and IV (a backing service, here the database, named by
# config alone). Precedence: the variable, then config.toml, then the default
# (``lestash.core.paths``). Applied by ``Config.load`` and never saved to the file.
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "LESTASH_DATABASE_PATH": ("general", "database_path"),
    "LESTASH_STORE_PATH": ("general", "store_path"),
    "LESTASH_LOG_FILE": ("logging", "file_path"),
}


class Config(BaseModel):
    """Application configuration.

    Besides ``[general]`` and ``[logging]``, each plugin reads its own table, e.g.
    ``[kobo]``; those are kept as they are on load and save (#208).
    """

    model_config = ConfigDict(extra="allow")

    # Built per Config, not once at import: the default paths follow the home folder
    # at the time of loading
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # The values the environment replaced, as (section, key) -> the value before it
    _overridden: dict[tuple[str, str], Any] = PrivateAttr(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        """Load config.toml, then apply ``ENV_OVERRIDES``: the environment wins."""
        config_path = get_config_path()
        if config_path.exists():
            with config_path.open("rb") as f:
                config = cls(**tomllib.load(f))
        else:
            config = cls()
        for variable, (section, key) in ENV_OVERRIDES.items():
            value = os.environ.get(variable, "")
            if value:
                part = getattr(config, section)
                config._overridden[(section, key)] = getattr(part, key)
                setattr(part, key, value)
        return config

    def save(self) -> None:
        """Save configuration to file, only if ``load`` reads it back the same.

        Written with tomli-w and read with tomllib, both TOML 1.0, so text with
        backslashes and lists mixing kinds survive the round trip.

        Raises:
            ValueError: the config can't be written as TOML that ``load`` reads back
                unchanged. The file is left as it was.
        """
        data = self.user_settings()
        try:
            text = tomli_w.dumps(data)
            reloaded = tomli_w.dumps(type(self)(**tomllib.loads(text)).user_settings())
        except Exception as e:
            raise ValueError(f"not saved: the config would not load again ({e!r})") from e
        # Compared as text, so a nan setting (nan != nan) doesn't block every save
        if reloaded != text:
            raise ValueError("not saved: the config would not load back the same")
        _replace_file(get_config_path(), text)

    def user_settings(self) -> dict[str, Any]:
        """The settings to save: every value but the defaults of ``[general]`` and ``[logging]``.

        A default is left out, so the file never pins one. The default database and
        log paths change with the folder layout (#304); a saved copy would hold the old
        place for good. A value set by the environment (``ENV_OVERRIDES``) is saved as
        it was before, so the file never holds it. Plugin tables are kept whole.
        """
        data = self.model_dump()
        for (section, key), before in self._overridden.items():
            data[section][key] = before
        for name, defaults in (("general", GeneralConfig()), ("logging", LoggingConfig())):
            given = data.pop(name)
            fresh = defaults.model_dump()
            kept = {key: value for key, value in given.items() if value != fresh.get(key)}
            if kept:
                data[name] = kept
        return data

    def get_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """The plugin's ``[plugin_name]`` table, or ``{}`` when it has none.

        A copy, so a plugin changing it can't change the config.
        """
        section = (self.model_extra or {}).get(plugin_name)
        return copy.deepcopy(section) if isinstance(section, dict) else {}


def custom_db_dir(config: Config) -> Path | None:
    """The database's folder when ``database_path`` is not the default, else None.

    The rule for derived places (#304): with the default database, its media, store,
    post cache, sync locks and backups are where ``lestash.core.paths`` says. A database
    set elsewhere keeps them beside itself, so a second database never shares them.
    """
    db_path = Path(config.general.database_path).expanduser()
    if db_path.resolve() == get_database_path().resolve():
        return None
    return db_path.parent


def init_config() -> Config:
    """Initialize configuration with defaults."""
    config = Config()
    config.save()
    return config

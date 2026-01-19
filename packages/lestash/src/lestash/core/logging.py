"""Centralized logging configuration for Le Stash.

This module provides:
- Rich console handler with pretty formatting
- File handler with rotation
- Optional database handler for queryable logs
- Easy logger acquisition for plugins
- Thread-safe operation for async httpx
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.logging import RichHandler

if TYPE_CHECKING:
    from lestash.core.config import Config, LoggingConfig

# Module-level state
_initialized = False
_console: Console | None = None


def get_console() -> Console:
    """Get the shared Rich console instance.

    This ensures logging and CLI output use the same console,
    preventing interleaved output issues. Logs go to stderr
    so they don't interfere with stdout output.
    """
    global _console
    if _console is None:
        _console = Console(stderr=True)
    return _console


class LeStashRichHandler(RichHandler):
    """Custom Rich handler with Le Stash formatting.

    Provides prettier output that matches existing CLI aesthetics.
    """

    def __init__(
        self,
        level: int | str = 0,
        show_time: bool = False,
        show_path: bool = False,
        **kwargs: Any,
    ) -> None:
        # Remove console from kwargs if present to avoid duplicate argument
        kwargs.pop("console", None)
        super().__init__(
            level=level,
            console=get_console(),
            show_time=show_time,
            show_path=show_path,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            markup=True,
            **kwargs,
        )


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging to files."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields if present
        if hasattr(record, "extra") and record.extra:
            log_data["extra"] = record.extra

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class DatabaseHandler(logging.Handler):
    """Log to SQLite database for queryable history.

    This handler writes log entries to the log_entries table.
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    def emit(self, record: logging.LogRecord) -> None:
        from lestash.core.database import get_connection

        try:
            extra_data = getattr(record, "extra", None)
            extra_json = json.dumps(extra_data) if extra_data else None

            with get_connection(self._config) as conn:
                conn.execute(
                    """
                    INSERT INTO log_entries (timestamp, level, logger, message, extra)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        record.levelname,
                        record.name,
                        record.getMessage(),
                        extra_json,
                    ),
                )
                conn.commit()
        except Exception:
            self.handleError(record)


def setup_logging(config: Config | None = None) -> None:
    """Initialize the logging system.

    Should be called once at application startup.
    Safe to call multiple times (idempotent).

    Args:
        config: Application configuration. If None, uses defaults.
    """
    global _initialized

    if _initialized:
        return

    if config is None:
        from lestash.core.config import Config

        config = Config.load()

    log_config = config.logging

    # Get the root lestash logger
    root_logger = logging.getLogger("lestash")
    root_logger.setLevel(getattr(logging, log_config.level))
    root_logger.handlers.clear()

    # Console handler (Rich) - logs to stderr
    if log_config.console_enabled:
        console_handler = LeStashRichHandler(
            show_time=log_config.console_timestamps,
            show_path=False,
        )
        console_handler.setLevel(getattr(logging, log_config.console_level))
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(console_handler)

    # File handler with rotation
    if log_config.file_enabled:
        log_path = Path(log_config.file_path).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=log_config.max_bytes,
            backupCount=log_config.backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(getattr(logging, log_config.file_level))

        # Format based on config
        if log_config.file_format == "json":
            file_handler.setFormatter(JsonFormatter())
        elif log_config.file_format == "detailed":
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        else:  # simple
            file_handler.setFormatter(logging.Formatter("%(message)s"))

        root_logger.addHandler(file_handler)

    # Database handler (optional)
    if log_config.db_enabled:
        db_handler = DatabaseHandler(config)
        db_handler.setLevel(getattr(logging, log_config.db_level))
        root_logger.addHandler(db_handler)

    # Apply filters for third-party loggers
    _apply_logger_filters(log_config)

    _initialized = True
    root_logger.debug("Logging initialized")


def _apply_logger_filters(log_config: LoggingConfig) -> None:
    """Apply log level filters to third-party loggers."""
    filters = log_config.filters

    for logger_name, level in filters.items():
        if isinstance(level, str) and level:
            logger = logging.getLogger(logger_name)
            logger.setLevel(getattr(logging, level, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a lestash module.

    Args:
        name: Logger name (will be prefixed with 'lestash.' if not already)

    Returns:
        Configured logger instance.

    Example:
        logger = get_logger("core.database")
        # Creates logger named "lestash.core.database"
    """
    setup_logging()  # Ensure initialized

    if name.startswith("lestash."):
        return logging.getLogger(name)
    return logging.getLogger(f"lestash.{name}")


def get_plugin_logger(plugin_name: str) -> logging.Logger:
    """Get a logger for a plugin.

    This is the primary API for plugin developers.

    Args:
        plugin_name: Plugin name (e.g., "linkedin", "arxiv")

    Returns:
        Configured logger for the plugin.

    Example:
        from lestash.core.logging import get_plugin_logger

        logger = get_plugin_logger("linkedin")
        logger.info("Starting sync")
        logger.debug("API response", extra={"status": 200})
    """
    setup_logging()  # Ensure initialized
    return logging.getLogger(f"lestash.plugins.{plugin_name}")

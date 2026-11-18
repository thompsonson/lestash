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
import sqlite3
import threading
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

    This handler writes log entries to the log_entries table. It opens a plain
    connection, never one that migrates, and waits at most WAIT_S for the write lock: a
    migration logs while holding the write lock and the handler's lock, so a longer wait
    stalls it, and a migrating connection would deadlock (#371). Records it cannot write
    yet are held and retried after RETRY_S, with the next record, or at exit.
    """

    # Records held while the database is busy; the oldest go first past this many
    MAX_HELD = 10_000
    WAIT_S = 1.0
    RETRY_S = 5.0
    _INSERT = (
        "INSERT INTO log_entries (timestamp, level, logger, message, extra) VALUES (?, ?, ?, ?, ?)"
    )

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config
        self._held: list[tuple[str, str, str, str, str | None]] = []
        self._retry: threading.Timer | None = None
        self._closed = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            extra_data = getattr(record, "extra", None)
            self._held.append(
                (
                    datetime.now(UTC).isoformat(),
                    record.levelname,
                    record.name,
                    record.getMessage(),
                    json.dumps(extra_data) if extra_data else None,
                )
            )
            del self._held[: -self.MAX_HELD]
            self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """Write the held records, oldest first.

        A busy, locked or missing database keeps them all for later. Any other error
        drops them: a record that cannot be written, or a database that cannot take
        any (read-only, full, broken), would otherwise be held and retried for good.
        """
        from lestash.core.database import get_db_path

        if not self._held:
            return
        self.acquire()
        try:
            uri = get_db_path(self._config).absolute().as_uri() + "?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=self.WAIT_S)
            try:
                try:
                    with conn:
                        conn.executemany(self._INSERT, self._held)
                    self._held = []
                except sqlite3.Error as e:
                    if _passing(e):
                        raise
                    self._write_one_by_one(conn)
                except Exception:
                    self._write_one_by_one(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            if not _passing(e):
                self._held = []  # the database cannot take them
        finally:
            if self._held:
                self._schedule_retry()
            self.release()

    def _schedule_retry(self) -> None:
        """Try the held records again later, once at a time."""
        if self._retry is not None or self._closed:
            return
        self._retry = threading.Timer(self.RETRY_S, self._retry_flush)
        self._retry.daemon = True  # never keeps the process alive; exit flushes
        self._retry.start()

    def _retry_flush(self) -> None:
        self.acquire()
        try:
            self._retry = None  # so a retry that fails schedules the next
            if not self._closed:
                self.flush()
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            self._closed = True  # no more retries; this flush is the last try
            if self._retry is not None:
                self._retry.cancel()
                self._retry = None
            self.flush()
        finally:
            self.release()
        super().close()

    def _write_one_by_one(self, conn: sqlite3.Connection) -> None:
        """Write the held records singly, dropping any that cannot be written."""
        while self._held:
            try:
                with conn:
                    conn.execute(self._INSERT, self._held[0])
            except sqlite3.Error as e:
                if _passing(e):
                    raise  # busy: keep this one and the rest
            except Exception:
                pass  # this record can never be written
            self._held.pop(0)


# SQLite errors that pass: the database is busy, locked, or not made yet
_PASSING_ERRORS = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN}


def _passing(e: sqlite3.Error) -> bool:
    """Whether a write that failed with e may work later."""
    code = getattr(e, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in _PASSING_ERRORS


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

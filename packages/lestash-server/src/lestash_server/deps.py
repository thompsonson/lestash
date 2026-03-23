"""Shared dependencies for the API server."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from lestash.core.config import Config
from lestash.core.database import get_connection

_config: Config | None = None


def get_config() -> Config:
    """Get or create the shared config instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Get a database connection via the shared config."""
    with get_connection(get_config()) as conn:
        yield conn

"""Core module for Le Stash."""

from lestash.core.logging import get_console, get_logger, get_plugin_logger, setup_logging

__all__ = [
    "get_console",
    "get_logger",
    "get_plugin_logger",
    "setup_logging",
]

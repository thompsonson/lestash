"""Plugin system for Le Stash."""

from lestash.plugins.base import SourcePlugin
from lestash.plugins.loader import load_plugins

__all__ = ["SourcePlugin", "load_plugins"]

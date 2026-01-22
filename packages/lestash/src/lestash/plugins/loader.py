"""Plugin discovery and loading."""

import time
from importlib.metadata import entry_points

from lestash.core.logging import get_logger
from lestash.plugins.base import SourcePlugin

logger = get_logger("plugins.loader")


def load_plugins() -> dict[str, SourcePlugin]:
    """Discover and load all installed source plugins.

    Returns:
        Dictionary mapping plugin names to plugin instances.
    """
    plugins: dict[str, SourcePlugin] = {}

    eps = entry_points(group="lestash.sources")
    for ep in eps:
        try:
            start_time = time.time()
            plugin_class = ep.load()
            plugin = plugin_class()
            elapsed = (time.time() - start_time) * 1000  # Convert to milliseconds
            plugins[ep.name] = plugin
            logger.debug(f"Loaded plugin '{ep.name}' ({elapsed:.1f}ms)")
        except Exception as e:
            logger.warning(f"Failed to load plugin '{ep.name}': {e}")

    return plugins

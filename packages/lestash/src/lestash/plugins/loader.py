"""Plugin discovery and loading."""

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
            plugin_class = ep.load()
            plugin = plugin_class()
            plugins[ep.name] = plugin
            logger.debug(f"Loaded plugin '{ep.name}'")
        except Exception as e:
            logger.warning(f"Failed to load plugin '{ep.name}': {e}")

    return plugins

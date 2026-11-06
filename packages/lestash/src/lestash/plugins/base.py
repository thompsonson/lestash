"""Base class for source plugins."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from lestash.models.item import ItemCreate
    from lestash.models.sync import SyncCheckpoint


class SourcePlugin(ABC):
    """Base class for all source plugins."""

    name: str
    description: str

    @property
    def logger(self) -> logging.Logger:
        """Get a logger for this plugin.

        Returns a logger named 'lestash.plugins.{plugin_name}'.
        """
        from lestash.core.logging import get_plugin_logger

        return get_plugin_logger(self.name)

    @abstractmethod
    def get_commands(self) -> typer.Typer:
        """Return a Typer app with commands to register.

        The returned Typer app will be added as a subcommand group
        using the plugin's name.
        """

    @abstractmethod
    def sync(self, config: dict) -> Iterator["ItemCreate | SyncCheckpoint"]:
        """Fetch new items from the source.

        Args:
            config: Plugin-specific configuration dict.

        Yields:
            ItemCreate instances for each new item. A plugin may also yield a
            SyncCheckpoint after a batch of items: it is committed with them.
        """

    def configure(self) -> dict:
        """Interactive configuration (optional).

        Returns:
            Configuration dict to be saved.
        """
        return {}

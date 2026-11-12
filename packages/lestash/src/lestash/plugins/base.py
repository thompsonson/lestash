"""Base class for source plugins."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from lestash.core.config import Config
    from lestash.models.item import ItemCreate
    from lestash.models.sync import SyncCheckpoint


class SourcePlugin(ABC):
    """Base class for all source plugins."""

    name: str
    description: str

    # Set by the caller before sync(): the app config whose database the sync writes to
    _app_config: "Config | None" = None

    @property
    def app_config(self) -> "Config":
        """The app config of the database being synced into.

        A plugin reads its database, file store and media folder through this, never
        through ``Config.load()``: the caller may sync into another database (a test,
        a trial on a backup copy). The CLI and server set it before calling ``sync()``.
        Unset (e.g. in a plugin's own commands), it is the default config.
        """
        if self._app_config is None:
            from lestash.core.config import Config

            return Config.load()
        return self._app_config

    @app_config.setter
    def app_config(self, config: "Config") -> None:
        self._app_config = config

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
            config: Plugin-specific configuration dict. The app config, for the
                database, is ``self.app_config``.

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

"""A plugin's own database reads use the caller's database, not the default (#231)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import typer
from lestash.cli import sources as sources_cli
from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from lestash.testing import forbid_default_config


class _ReadsItsDatabase(SourcePlugin):
    """Counts the stored items in its database, as arXiv reads its saved papers."""

    name = "probe"
    description = "probe"

    def __init__(self) -> None:
        self.seen: list[int] = []

    def get_commands(self) -> typer.Typer:
        return typer.Typer()

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        with get_connection(self.app_config) as conn:
            self.seen.append(conn.execute("SELECT count(*) FROM items").fetchone()[0])
        yield ItemCreate(source_type="probe", source_id=f"probe:{len(self.seen)}", content="x")


def _default_db() -> Path:
    return Path.home() / ".config" / "lestash" / "lestash.db"


def test_app_config_is_the_default_until_set(test_db, monkeypatch) -> None:
    plugin = _ReadsItsDatabase()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: test_db))
    assert plugin.app_config is test_db  # unset: the default config, for plugin commands
    other = Config()
    plugin.app_config = other
    forbid_default_config(monkeypatch, test_db)
    assert plugin.app_config is other


def test_cli_sync_hands_the_plugin_its_config(test_db, monkeypatch) -> None:
    plugin = _ReadsItsDatabase()
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"probe": plugin})
    forbid_default_config(monkeypatch, test_db, allowed=1)  # the CLI's own load
    sources_cli.sync_source(source_name="probe", all_sources=False)
    forbid_default_config(monkeypatch, test_db, allowed=1)
    sources_cli.sync_source(source_name="probe", all_sources=False)
    # The second sync's read saw the first sync's item: both read the caller's database
    assert plugin.seen == [0, 1]
    with get_connection(test_db) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert not _default_db().exists()

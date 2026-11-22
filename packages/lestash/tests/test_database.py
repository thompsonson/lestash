"""Tests for creating a new database (#405)."""

import sqlite3

from lestash.core import database
from lestash.core.config import Config, GeneralConfig


def _config(path):
    return Config(general=GeneralConfig(database_path=str(path)))


def test_new_database_copies_the_template(tmp_path, monkeypatch):
    """A new database is a copy of the template, migrated, with no temp copy left."""
    template = tmp_path / "template.db"
    database.init_database(_config(template))
    with sqlite3.connect(template) as conn:
        conn.execute("CREATE TABLE marker (x)")
    monkeypatch.setattr(database, "template_database", template)

    config = _config(tmp_path / "new.db")
    database.init_database(config)
    with database.get_connection(config) as conn:
        assert database.get_schema_version(conn) == database.SCHEMA_VERSION
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'marker'").fetchone()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".new.db.")]


def test_copy_never_overwrites_an_existing_file(tmp_path):
    assert database.template_database is not None
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"")
    database._copy_template(database.template_database, existing)
    assert existing.read_bytes() == b""


def test_no_hard_links_falls_back_to_migrating(tmp_path, monkeypatch):
    def no_link(src, dst):
        raise PermissionError("hard links not supported")

    monkeypatch.setattr(database.os, "link", no_link)
    config = _config(tmp_path / "new.db")
    database.init_database(config)
    with database.get_connection(config) as conn:
        assert database.get_schema_version(conn) == database.SCHEMA_VERSION

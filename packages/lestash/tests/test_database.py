"""Tests for creating a new database (#405)."""

from lestash.core import database
from lestash.core.config import Config, GeneralConfig


def test_new_database_copies_template_and_keeps_existing(tmp_path):
    """A new database copies the session's template; an existing file is left alone (#405)."""

    assert database.template_database is not None
    db_path = tmp_path / "new.db"
    config = Config(general=GeneralConfig(database_path=str(db_path)))
    database.init_database(config)
    with database.get_connection(config) as conn:
        assert database.get_schema_version(conn) == database.SCHEMA_VERSION
    assert [p.name for p in tmp_path.iterdir()] == ["new.db"]  # no temp copy left

    existing = tmp_path / "existing.db"
    existing.write_bytes(b"")
    database._copy_template(database.template_database, existing)
    assert existing.read_bytes() == b""

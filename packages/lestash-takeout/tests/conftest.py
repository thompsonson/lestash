"""Fixtures for lestash-takeout: a temporary LeStash database."""

import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import init_database


@pytest.fixture
def test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "test.db")))
        init_database(config)
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
        yield config

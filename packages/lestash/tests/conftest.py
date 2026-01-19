"""Test fixtures for lestash core package."""

import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import init_database


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)
        yield config

"""Test fixtures for lestash-server."""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import lestash_server.deps as deps
import pytest
from fastapi.testclient import TestClient
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database
from lestash_server.app import create_app


@pytest.fixture
def test_config():
    """Create a temporary database with test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)

        # Seed test data
        with get_connection(config) as conn:
            items = [
                (
                    "linkedin",
                    "li-1",
                    "https://linkedin.com/post/1",
                    "First Post",
                    "My first LinkedIn post about Python",
                    "urn:li:person:author1",
                    datetime(2024, 1, 15),
                    True,
                    json.dumps({"resource_name": "ugcPosts"}),
                ),
                (
                    "linkedin",
                    "li-2",
                    None,
                    None,
                    "👍 LIKE on activity:123",
                    "urn:li:person:author1",
                    datetime(2024, 2, 10),
                    False,
                    json.dumps(
                        {
                            "resource_name": "socialActions/likes",
                            "reaction_type": "LIKE",
                            "reacted_to": "urn:li:activity:123",
                        }
                    ),
                ),
                (
                    "bluesky",
                    "bs-1",
                    "https://bsky.app/post/1",
                    "Bluesky Thoughts",
                    "Thinking about decentralized social media",
                    "did:plc:user1",
                    datetime(2024, 3, 5),
                    True,
                    None,
                ),
                (
                    "youtube",
                    "yt-1",
                    "https://youtube.com/watch?v=abc",
                    "Python Tutorial",
                    "Great tutorial on FastAPI",
                    None,
                    datetime(2024, 4, 20),
                    False,
                    json.dumps({"channel": "TechChannel"}),
                ),
                (
                    "arxiv",
                    "2401.12345",
                    "https://arxiv.org/abs/2401.12345",
                    "Attention Is All You Need (Again)",
                    "A new perspective on transformer architectures",
                    "Author et al.",
                    datetime(2024, 5, 1),
                    False,
                    None,
                ),
            ]
            for item in items:
                conn.execute(
                    """INSERT INTO items (source_type, source_id, url, title, content,
                       author, created_at, is_own_content, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    item,
                )
            conn.commit()

        yield config


@pytest.fixture
def client(test_config):
    """Create a test client with a temporary database."""
    # Override the global config in deps
    original_config = deps._config
    deps._config = test_config

    app = create_app()
    with TestClient(app) as tc:
        yield tc

    # Restore
    deps._config = original_config

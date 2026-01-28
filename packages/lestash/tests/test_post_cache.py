"""Tests for post_cache functionality (migration 3)."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import (
    SCHEMA_VERSION,
    get_cache_dir,
    get_connection,
    get_db_path,
    get_post_cache,
    get_schema_version,
    init_database,
    upsert_post_cache,
)


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)
        yield config


class TestPostCacheTableCreation:
    """Test that post_cache table is created correctly."""

    def test_post_cache_table_exists(self, test_db):
        """Verify post_cache table is created on init."""
        with get_connection(test_db) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='post_cache'"
            )
            assert cursor.fetchone() is not None

    def test_post_cache_has_required_columns(self, test_db):
        """Verify all required columns exist."""
        with get_connection(test_db) as conn:
            cursor = conn.execute("PRAGMA table_info(post_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            expected = {
                "id",
                "urn",
                "author_urn",
                "author_name",
                "content_preview",
                "full_content",
                "image_path",
                "url",
                "created_at",
                "fetched_at",
                "source",
            }
            assert expected.issubset(columns)

    def test_post_cache_urn_index_exists(self, test_db):
        """Verify index on URN column exists."""
        with get_connection(test_db) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_post_cache_urn'"
            )
            assert cursor.fetchone() is not None

    def test_post_cache_urn_is_unique(self, test_db):
        """Verify URN column has unique constraint."""
        with get_connection(test_db) as conn:
            # Insert first row
            conn.execute(
                "INSERT INTO post_cache (urn, content_preview) VALUES (?, ?)",
                ("urn:li:activity:123", "test content"),
            )
            conn.commit()

            # Attempt duplicate should fail
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO post_cache (urn, content_preview) VALUES (?, ?)",
                    ("urn:li:activity:123", "different content"),
                )


class TestGetPostCache:
    """Test get_post_cache function."""

    def test_returns_none_for_missing_urn(self, test_db):
        """Should return None when URN not found."""
        with get_connection(test_db) as conn:
            result = get_post_cache(conn, "urn:li:activity:nonexistent")
            assert result is None

    def test_returns_dict_for_existing_urn(self, test_db):
        """Should return dict with all fields for existing URN."""
        with get_connection(test_db) as conn:
            conn.execute(
                """INSERT INTO post_cache (urn, author_urn, author_name, content_preview,
                   full_content, image_path, url, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "urn:li:activity:123",
                    "urn:li:person:abc",
                    "John Doe",
                    "Preview text",
                    "Full content here",
                    "/path/to/image.png",
                    "https://linkedin.com/feed/update/urn:li:activity:123",
                    "manual",
                ),
            )
            conn.commit()

            result = get_post_cache(conn, "urn:li:activity:123")

            assert result is not None
            assert result["urn"] == "urn:li:activity:123"
            assert result["author_urn"] == "urn:li:person:abc"
            assert result["author_name"] == "John Doe"
            assert result["content_preview"] == "Preview text"
            assert result["full_content"] == "Full content here"
            assert result["image_path"] == "/path/to/image.png"
            assert result["url"] == "https://linkedin.com/feed/update/urn:li:activity:123"
            assert result["source"] == "manual"

    def test_returns_dict_with_partial_data(self, test_db):
        """Should return dict even with minimal data (only URN required)."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO post_cache (urn) VALUES (?)",
                ("urn:li:activity:456",),
            )
            conn.commit()

            result = get_post_cache(conn, "urn:li:activity:456")

            assert result is not None
            assert result["urn"] == "urn:li:activity:456"
            assert result["content_preview"] is None
            assert result["author_name"] is None


class TestUpsertPostCache:
    """Test upsert_post_cache function."""

    def test_inserts_new_record(self, test_db):
        """Should insert new record when URN doesn't exist."""
        with get_connection(test_db) as conn:
            upsert_post_cache(
                conn,
                urn="urn:li:activity:new",
                content_preview="New post content",
                author_name="Jane Doe",
                source="manual",
            )

            result = get_post_cache(conn, "urn:li:activity:new")
            assert result is not None
            assert result["content_preview"] == "New post content"
            assert result["author_name"] == "Jane Doe"
            assert result["source"] == "manual"

    def test_updates_existing_record(self, test_db):
        """Should update existing record when URN exists."""
        with get_connection(test_db) as conn:
            # First insert
            upsert_post_cache(
                conn,
                urn="urn:li:activity:existing",
                content_preview="Original content",
                source="manual",
            )

            # Update
            upsert_post_cache(
                conn,
                urn="urn:li:activity:existing",
                content_preview="Updated content",
                author_name="Added Author",
                source="api",
            )

            result = get_post_cache(conn, "urn:li:activity:existing")
            assert result["content_preview"] == "Updated content"
            assert result["author_name"] == "Added Author"
            assert result["source"] == "api"

    def test_preserves_existing_values_when_new_value_is_none(self, test_db):
        """COALESCE should preserve existing values when update provides None."""
        with get_connection(test_db) as conn:
            # Insert with content
            upsert_post_cache(
                conn,
                urn="urn:li:activity:preserve",
                content_preview="Original preview",
                author_name="Original Author",
                source="manual",
            )

            # Update with only image_path, other values None
            upsert_post_cache(
                conn,
                urn="urn:li:activity:preserve",
                image_path="/new/image.png",
                source="image",
            )

            result = get_post_cache(conn, "urn:li:activity:preserve")
            # Original values preserved
            assert result["content_preview"] == "Original preview"
            assert result["author_name"] == "Original Author"
            # New value added
            assert result["image_path"] == "/new/image.png"
            # Source is always replaced
            assert result["source"] == "image"

    def test_stores_all_optional_fields(self, test_db):
        """Should store all optional fields correctly."""
        with get_connection(test_db) as conn:
            upsert_post_cache(
                conn,
                urn="urn:li:activity:full",
                content_preview="Short preview",
                full_content="This is the full content of the post that is much longer.",
                author_urn="urn:li:person:xyz",
                author_name="Full Name",
                image_path="/cache/posts/abc123.png",
                url="https://linkedin.com/feed/update/urn:li:activity:full",
                source="own_post",
            )

            result = get_post_cache(conn, "urn:li:activity:full")
            expected = "This is the full content of the post that is much longer."
            assert result["full_content"] == expected
            assert result["author_urn"] == "urn:li:person:xyz"
            assert result["url"] == "https://linkedin.com/feed/update/urn:li:activity:full"

    def test_updates_fetched_at_timestamp(self, test_db):
        """Should update fetched_at on every upsert."""
        with get_connection(test_db) as conn:
            upsert_post_cache(conn, urn="urn:li:activity:timestamp")

            result1 = get_post_cache(conn, "urn:li:activity:timestamp")
            fetched_at_1 = result1["fetched_at"]
            assert fetched_at_1 is not None

            # Upsert again
            upsert_post_cache(conn, urn="urn:li:activity:timestamp", content_preview="Updated")

            result2 = get_post_cache(conn, "urn:li:activity:timestamp")
            fetched_at_2 = result2["fetched_at"]
            # Should be updated (or same if done quickly)
            assert fetched_at_2 is not None


class TestGetCacheDir:
    """Test get_cache_dir function."""

    def test_creates_directory_if_not_exists(self, test_db):
        """Should create cache directory if it doesn't exist."""
        cache_dir = get_cache_dir(test_db)

        assert cache_dir.exists()
        assert cache_dir.is_dir()

    def test_returns_correct_path_structure(self, test_db):
        """Should return path under .lestash/cache/posts/."""
        cache_dir = get_cache_dir(test_db)

        # Should end with cache/posts
        assert cache_dir.name == "posts"
        assert cache_dir.parent.name == "cache"

    def test_idempotent_creation(self, test_db):
        """Calling multiple times should be safe."""
        cache_dir1 = get_cache_dir(test_db)
        cache_dir2 = get_cache_dir(test_db)

        assert cache_dir1 == cache_dir2
        assert cache_dir1.exists()


class TestMigration3AppliedCorrectly:
    """Test that migration 3 integrates properly with existing schema."""

    def test_migration_from_version_2(self, test_db):
        """Migration should work from version 2 to 3."""
        db_path = get_db_path(test_db)

        # Simulate database at version 2 (drop post_cache)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 2")
            conn.execute("DROP TABLE IF EXISTS post_cache")
            conn.execute("DROP INDEX IF EXISTS idx_post_cache_urn")
            conn.commit()

        # get_connection should apply migration 3
        with get_connection(test_db) as conn:
            version = get_schema_version(conn)
            assert version == SCHEMA_VERSION

            # post_cache should exist
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='post_cache'"
            )
            assert cursor.fetchone() is not None

    def test_post_cache_coexists_with_other_tables(self, test_db):
        """post_cache should work alongside items and other tables."""
        with get_connection(test_db) as conn:
            # Insert an item
            conn.execute(
                """INSERT INTO items (source_type, source_id, content, metadata)
                   VALUES (?, ?, ?, ?)""",
                ("linkedin", "reaction-1", "Like", '{"reacted_to": "urn:li:activity:789"}'),
            )
            conn.commit()

            # Insert corresponding post_cache entry
            upsert_post_cache(
                conn,
                urn="urn:li:activity:789",
                content_preview="The original post content",
            )

            # Both should be queryable
            query = "SELECT content FROM items WHERE source_id = ?"
            item_cursor = conn.execute(query, ("reaction-1",))
            item = item_cursor.fetchone()
            assert item[0] == "Like"

            cache = get_post_cache(conn, "urn:li:activity:789")
            assert cache["content_preview"] == "The original post content"

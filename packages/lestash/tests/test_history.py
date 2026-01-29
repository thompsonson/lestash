"""Tests for item history tracking and schema migrations."""

import sqlite3

from lestash.core.database import (
    SCHEMA_VERSION,
    apply_migrations,
    get_connection,
    get_db_path,
    get_schema_version,
    upsert_item,
)


class TestSchemaMigrations:
    """Test schema versioning and migrations."""

    def test_new_database_has_current_version(self, test_db):
        """New databases should have the current schema version."""
        with get_connection(test_db) as conn:
            version = get_schema_version(conn)
            assert version == SCHEMA_VERSION

    def test_apply_migrations_is_idempotent(self, test_db):
        """Calling apply_migrations multiple times should be safe."""
        with get_connection(test_db) as conn:
            # Apply migrations twice
            apply_migrations(conn)
            applied2 = apply_migrations(conn)

            # Second call should apply nothing
            assert applied2 == 0

            # Version should still be current
            assert get_schema_version(conn) == SCHEMA_VERSION

    def test_migrations_applied_on_connection(self, test_db):
        """get_connection should automatically apply pending migrations."""
        # Manually reset version to 0 (simulating old database)
        db_path = get_db_path(test_db)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 0")
            # Drop the history table to simulate pre-migration state
            conn.execute("DROP TABLE IF EXISTS item_history")
            conn.execute("DROP TRIGGER IF EXISTS capture_item_history")
            conn.commit()

        # Now get_connection should apply migrations
        with get_connection(test_db) as conn:
            version = get_schema_version(conn)
            assert version == SCHEMA_VERSION

            # History table should exist
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='item_history'"
            )
            assert cursor.fetchone() is not None


class TestHistoryTableCreation:
    """Test that history table is created correctly."""

    def test_item_history_table_exists(self, test_db):
        """Verify item_history table is created on init."""
        with get_connection(test_db) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='item_history'"
            )
            assert cursor.fetchone() is not None

    def test_item_history_has_required_columns(self, test_db):
        """Verify all required columns exist."""
        with get_connection(test_db) as conn:
            cursor = conn.execute("PRAGMA table_info(item_history)")
            columns = {row[1] for row in cursor.fetchall()}
            expected = {
                "id",
                "item_id",
                "content_old",
                "title_old",
                "author_old",
                "url_old",
                "metadata_old",
                "is_own_content_old",
                "changed_at",
                "change_reason",
                "change_type",
            }
            assert expected.issubset(columns)


class TestHistoryTrigger:
    """Test automatic history capture on updates."""

    def test_trigger_captures_content_change(self, test_db):
        """Updating content should create history record."""
        with get_connection(test_db) as conn:
            # Insert item
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "id1", "old content"),
            )
            conn.commit()

            # Update content
            conn.execute(
                "UPDATE items SET content = ? WHERE source_id = ?",
                ("new content", "id1"),
            )
            conn.commit()

            # Verify history captured
            cursor = conn.execute("SELECT content_old FROM item_history WHERE item_id = 1")
            history = cursor.fetchone()
            assert history is not None
            assert history[0] == "old content"

    def test_trigger_captures_author_change(self, test_db):
        """Updating author should create history record."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content, author) VALUES (?, ?, ?, ?)",
                ("test", "id1", "content", "old_author"),
            )
            conn.commit()

            conn.execute(
                "UPDATE items SET author = ? WHERE source_id = ?",
                ("new_author", "id1"),
            )
            conn.commit()

            cursor = conn.execute("SELECT author_old FROM item_history WHERE item_id = 1")
            history = cursor.fetchone()
            assert history[0] == "old_author"

    def test_trigger_captures_metadata_change(self, test_db):
        """Updating metadata should create history record."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content, metadata) VALUES (?, ?, ?, ?)",
                ("test", "id1", "content", '{"old": "data"}'),
            )
            conn.commit()

            conn.execute(
                "UPDATE items SET metadata = ? WHERE source_id = ?",
                ('{"new": "data"}', "id1"),
            )
            conn.commit()

            cursor = conn.execute("SELECT metadata_old FROM item_history WHERE item_id = 1")
            history = cursor.fetchone()
            assert history[0] == '{"old": "data"}'

    def test_trigger_does_not_fire_on_no_change(self, test_db):
        """No history record if content/author/metadata unchanged."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content, url) VALUES (?, ?, ?, ?)",
                ("test", "id1", "content", "http://old.url"),
            )
            conn.commit()

            # Update only URL (not tracked by trigger)
            conn.execute(
                "UPDATE items SET url = ? WHERE source_id = ?",
                ("http://new.url", "id1"),
            )
            conn.commit()

            cursor = conn.execute("SELECT COUNT(*) FROM item_history")
            assert cursor.fetchone()[0] == 0

    def test_trigger_captures_changed_at_timestamp(self, test_db):
        """History record should have timestamp."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "id1", "old"),
            )
            conn.commit()

            conn.execute("UPDATE items SET content = 'new' WHERE source_id = 'id1'")
            conn.commit()

            cursor = conn.execute("SELECT changed_at FROM item_history WHERE item_id = 1")
            changed_at = cursor.fetchone()[0]
            assert changed_at is not None


class TestHistoryWithUpsert:
    """Test history works with upsert_item helper."""

    def test_upsert_triggers_history_on_update(self, test_db):
        """Upsert that updates should create history."""
        with get_connection(test_db) as conn:
            # First insert
            conn.execute(
                """INSERT INTO items (source_type, source_id, content, author)
                   VALUES (?, ?, ?, ?)""",
                ("linkedin", "post-123", "CREATE ugcPosts", None),
            )
            conn.commit()

            # Upsert with new content (triggers UPDATE path)
            upsert_item(
                conn,
                source_type="linkedin",
                source_id="post-123",
                content="Actual post content here",
                author="urn:li:person:abc",
            )
            conn.commit()

            # Verify history
            cursor = conn.execute("SELECT content_old, author_old FROM item_history")
            history = cursor.fetchone()
            assert history[0] == "CREATE ugcPosts"
            assert history[1] is None

    def test_upsert_insert_does_not_create_history(self, test_db):
        """Upsert that inserts (no existing row) should not create history."""
        with get_connection(test_db) as conn:
            upsert_item(
                conn,
                source_type="linkedin",
                source_id="new-post",
                content="Fresh content",
            )
            conn.commit()

            cursor = conn.execute("SELECT COUNT(*) FROM item_history")
            assert cursor.fetchone()[0] == 0


class TestHistoryCascadeDelete:
    """Test history cleanup when items deleted."""

    def test_history_deleted_when_item_deleted(self, test_db):
        """History records should be deleted with item (CASCADE)."""
        with get_connection(test_db) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "id1", "old"),
            )
            conn.commit()

            conn.execute("UPDATE items SET content = 'new' WHERE source_id = 'id1'")
            conn.commit()

            # Verify history exists
            cursor = conn.execute("SELECT COUNT(*) FROM item_history")
            assert cursor.fetchone()[0] == 1

            # Delete item
            conn.execute("DELETE FROM items WHERE source_id = 'id1'")
            conn.commit()

            # History should be gone
            cursor = conn.execute("SELECT COUNT(*) FROM item_history")
            assert cursor.fetchone()[0] == 0

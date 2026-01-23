"""Tests for cr-sqlite CRDT sync functionality.

These tests verify actual sync behavior - that data correctly moves between
databases and merges properly using CRDTs.

Tests requiring the cr-sqlite extension are marked with @pytest.mark.crsqlite
and will be skipped if the extension is not available.
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from lestash.core import crsqlite
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, get_crdt_connection, init_database


# Check if cr-sqlite extension is available for integration tests
def crsqlite_available() -> bool:
    """Check if cr-sqlite can be loaded."""
    if not crsqlite.is_extension_available():
        return False
    # Also verify it actually loads
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = sqlite3.connect(db_path)
            result = crsqlite.load_extension(conn)
            if result:
                crsqlite.finalize_connection(conn)
            conn.close()
            return result
    except Exception:
        return False


# Marker for tests requiring cr-sqlite extension
crsqlite_required = pytest.mark.skipif(
    not crsqlite_available(),
    reason="cr-sqlite extension not available",
)


@pytest.fixture
def crdt_db():
    """Create a temporary database with cr-sqlite loaded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)
        yield config


@pytest.fixture
def two_crdt_dbs():
    """Create two separate databases for sync testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path_a = Path(tmpdir) / "db_a.db"
        db_path_b = Path(tmpdir) / "db_b.db"

        config_a = Config(general=GeneralConfig(database_path=str(db_path_a)))
        config_b = Config(general=GeneralConfig(database_path=str(db_path_b)))

        init_database(config_a)
        init_database(config_b)

        yield config_a, config_b


class TestPlatformDetection:
    """Test platform detection logic - no extension needed."""

    def test_get_platform_info_returns_tuple(self):
        """Platform info should return system and machine."""
        system, machine = crsqlite.get_platform_info()
        assert isinstance(system, str)
        assert isinstance(machine, str)
        assert len(system) > 0
        assert len(machine) > 0

    def test_extension_path_uses_correct_suffix(self):
        """Extension path should have platform-appropriate suffix."""
        ext_path = crsqlite.get_extension_path()
        if ext_path is None:
            pytest.skip("Platform not supported")

        system, _ = crsqlite.get_platform_info()
        if system == "Darwin":
            assert ext_path.suffix == ".dylib"
        elif system == "Linux":
            assert ext_path.suffix == ".so"
        elif system == "Windows":
            assert ext_path.suffix == ".dll"

    def test_platform_map_has_expected_platforms(self):
        """Verify we support the common platforms."""
        expected_platforms = [
            ("Darwin", "x86_64"),
            ("Darwin", "arm64"),
            ("Linux", "x86_64"),
            ("Linux", "aarch64"),
        ]
        for platform in expected_platforms:
            assert platform in crsqlite.PLATFORM_MAP


class TestExportFileFormat:
    """Test export file format without requiring cr-sqlite."""

    def test_export_format_structure(self, crdt_db, tmp_path):
        """Exported JSON should have required structure."""
        if not crsqlite_available():
            pytest.skip("cr-sqlite extension not available")

        output_file = tmp_path / "export.json"

        with get_crdt_connection(crdt_db) as conn:
            # Upgrade items table to CRR
            crsqlite.upgrade_to_crr(conn, "items")

            # Export (even with no data, format should be valid)
            crsqlite.export_changes_to_file(conn, output_file, since_version=0)

        # Verify file was created
        assert output_file.exists()

        # Verify JSON structure
        data = json.loads(output_file.read_text())
        assert data["format"] == "lestash-crsqlite-v1"
        assert "site_id" in data
        assert "db_version" in data
        assert "since_version" in data
        assert "change_count" in data
        assert "changes" in data
        assert isinstance(data["changes"], list)

    def test_import_rejects_invalid_format(self, crdt_db, tmp_path):
        """Importing file with wrong format should raise ValueError."""
        if not crsqlite_available():
            pytest.skip("cr-sqlite extension not available")

        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps({"format": "wrong-format", "changes": []}))

        with get_crdt_connection(crdt_db) as conn, pytest.raises(
            ValueError, match="Unknown export format"
        ):
            crsqlite.import_changes_from_file(conn, bad_file)

    def test_import_nonexistent_file_raises(self, crdt_db, tmp_path):
        """Importing non-existent file should raise FileNotFoundError."""
        if not crsqlite_available():
            pytest.skip("cr-sqlite extension not available")

        missing_file = tmp_path / "does_not_exist.json"

        with get_crdt_connection(crdt_db) as conn, pytest.raises(FileNotFoundError):
            crsqlite.import_changes_from_file(conn, missing_file)


@crsqlite_required
class TestChangesetRoundtrip:
    """Test that data correctly syncs between two databases."""

    def test_item_syncs_from_a_to_b(self, two_crdt_dbs):
        """Item created in DB A should appear in DB B after sync."""
        config_a, config_b = two_crdt_dbs

        # Create item in DB A
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.upgrade_to_crr(conn_a, "items")

            conn_a.execute(
                "INSERT INTO items (source_type, source_id, content, title) VALUES (?, ?, ?, ?)",
                ("test", "item-1", "Hello from DB A", "Test Title"),
            )
            conn_a.commit()

            # Get changes
            changes = crsqlite.get_changes_since(conn_a, since_version=0)
            assert len(changes) > 0

        # Apply to DB B
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.upgrade_to_crr(conn_b, "items")

            applied = crsqlite.apply_changes(conn_b, changes)
            assert applied > 0

            # Verify item exists in DB B
            cursor = conn_b.execute(
                "SELECT content, title FROM items WHERE source_id = ?", ("item-1",)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "Hello from DB A"
            assert row[1] == "Test Title"

    def test_bidirectional_sync_merges_different_items(self, two_crdt_dbs):
        """Items created on different DBs should both exist after bidirectional sync."""
        config_a, config_b = two_crdt_dbs

        # Create item in DB A
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.upgrade_to_crr(conn_a, "items")
            conn_a.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "from-a", "Created on A"),
            )
            conn_a.commit()
            changes_a = crsqlite.get_changes_since(conn_a, since_version=0)

        # Create different item in DB B
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.upgrade_to_crr(conn_b, "items")
            conn_b.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "from-b", "Created on B"),
            )
            conn_b.commit()
            changes_b = crsqlite.get_changes_since(conn_b, since_version=0)

        # Sync A -> B
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.apply_changes(conn_b, changes_a)

            # B should have both items
            cursor = conn_b.execute("SELECT source_id FROM items ORDER BY source_id")
            rows = cursor.fetchall()
            source_ids = [r[0] for r in rows]
            assert "from-a" in source_ids
            assert "from-b" in source_ids

        # Sync B -> A
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.apply_changes(conn_a, changes_b)

            # A should have both items
            cursor = conn_a.execute("SELECT source_id FROM items ORDER BY source_id")
            rows = cursor.fetchall()
            source_ids = [r[0] for r in rows]
            assert "from-a" in source_ids
            assert "from-b" in source_ids

    def test_export_import_file_roundtrip(self, two_crdt_dbs, tmp_path):
        """Data exported to file and imported should match original."""
        config_a, config_b = two_crdt_dbs
        export_file = tmp_path / "sync.json"

        # Create items in DB A and export
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.upgrade_to_crr(conn_a, "items")

            conn_a.execute(
                "INSERT INTO items (source_type, source_id, content, author) VALUES (?, ?, ?, ?)",
                ("linkedin", "post-123", "My LinkedIn post", "john.doe"),
            )
            conn_a.execute(
                "INSERT INTO items (source_type, source_id, content, title) VALUES (?, ?, ?, ?)",
                ("arxiv", "2401.00001", "Abstract text", "Paper Title"),
            )
            conn_a.commit()

            export_result = crsqlite.export_changes_to_file(conn_a, export_file)
            assert export_result["change_count"] > 0

        # Import to DB B
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.upgrade_to_crr(conn_b, "items")

            import_result = crsqlite.import_changes_from_file(conn_b, export_file)
            assert import_result["changes_applied"] > 0

            # Verify both items exist with correct data
            cursor = conn_b.execute(
                """SELECT source_type, source_id, content, author, title
                   FROM items ORDER BY source_id"""
            )
            rows = cursor.fetchall()

            # Convert to dict for easier assertion
            items = {
                r[1]: {"type": r[0], "content": r[2], "author": r[3], "title": r[4]}
                for r in rows
            }

            assert "post-123" in items
            assert items["post-123"]["content"] == "My LinkedIn post"
            assert items["post-123"]["author"] == "john.doe"

            assert "2401.00001" in items
            assert items["2401.00001"]["content"] == "Abstract text"
            assert items["2401.00001"]["title"] == "Paper Title"


@crsqlite_required
class TestFTSRebuild:
    """Test that FTS index is correctly rebuilt after sync."""

    def test_fts_search_works_after_sync(self, two_crdt_dbs):
        """Items synced to DB B should be searchable via FTS."""
        config_a, config_b = two_crdt_dbs

        # Create searchable item in DB A
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.upgrade_to_crr(conn_a, "items")

            conn_a.execute(
                "INSERT INTO items (source_type, source_id, content, title) VALUES (?, ?, ?, ?)",
                ("test", "unique-item", "quantum computing research paper", "Quantum Mechanics"),
            )
            conn_a.commit()

            changes = crsqlite.get_changes_since(conn_a, since_version=0)

        # Sync to DB B and rebuild FTS
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.upgrade_to_crr(conn_b, "items")
            crsqlite.apply_changes(conn_b, changes)

            # Rebuild FTS
            count = crsqlite.rebuild_fts_index(conn_b)
            assert count == 1

            # Search should find the item
            cursor = conn_b.execute(
                """
                SELECT items.source_id FROM items
                JOIN items_fts ON items.id = items_fts.rowid
                WHERE items_fts MATCH ?
                """,
                ("quantum",),
            )
            results = cursor.fetchall()
            assert len(results) == 1
            assert results[0][0] == "unique-item"

    def test_fts_rebuild_indexes_all_items(self, crdt_db):
        """FTS rebuild should index all items in the table."""
        if not crsqlite_available():
            pytest.skip("cr-sqlite extension not available")

        with get_crdt_connection(crdt_db) as conn:
            crsqlite.upgrade_to_crr(conn, "items")

            # Insert multiple items
            items_data = [
                ("test", "item-1", "alpha beta gamma"),
                ("test", "item-2", "delta epsilon zeta"),
                ("test", "item-3", "eta theta iota"),
            ]
            for source_type, source_id, content in items_data:
                conn.execute(
                    "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                    (source_type, source_id, content),
                )
            conn.commit()

            # Clear FTS and rebuild
            conn.execute("DELETE FROM items_fts")
            conn.commit()

            count = crsqlite.rebuild_fts_index(conn)
            assert count == 3

            # Verify each item is searchable
            search_terms = [("alpha", "item-1"), ("delta", "item-2"), ("theta", "item-3")]
            for term, expected_id in search_terms:
                cursor = conn.execute(
                    """
                    SELECT items.source_id FROM items
                    JOIN items_fts ON items.id = items_fts.rowid
                    WHERE items_fts MATCH ?
                    """,
                    (term,),
                )
                results = cursor.fetchall()
                assert len(results) == 1
                assert results[0][0] == expected_id


@crsqlite_required
class TestCRDTMerge:
    """Test CRDT merge behavior for concurrent updates."""

    def test_concurrent_updates_to_different_fields_merge(self, two_crdt_dbs):
        """Updates to different fields should both be preserved."""
        config_a, config_b = two_crdt_dbs

        # Create item in both DBs (simulating initial sync)
        for config in [config_a, config_b]:
            with get_crdt_connection(config) as conn:
                crsqlite.upgrade_to_crr(conn, "items")
                conn.execute(
                    """INSERT INTO items
                       (source_type, source_id, content, title, author)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("test", "shared-item", "original", "original title", "original author"),
                )
                conn.commit()

        # Get initial state synced
        with get_crdt_connection(config_a) as conn_a:
            changes_a = crsqlite.get_changes_since(conn_a, since_version=0)
        with get_crdt_connection(config_b) as conn_b:
            crsqlite.apply_changes(conn_b, changes_a)

        # DB A updates title
        with get_crdt_connection(config_a) as conn_a:
            conn_a.execute(
                "UPDATE items SET title = ? WHERE source_id = ?",
                ("updated title from A", "shared-item"),
            )
            conn_a.commit()
            changes_a_title = crsqlite.get_changes_since(conn_a, since_version=0)

        # DB B updates author (different field)
        with get_crdt_connection(config_b) as conn_b:
            conn_b.execute(
                "UPDATE items SET author = ? WHERE source_id = ?",
                ("updated author from B", "shared-item"),
            )
            conn_b.commit()
            changes_b_author = crsqlite.get_changes_since(conn_b, since_version=0)

        # Sync both ways
        with get_crdt_connection(config_a) as conn_a:
            crsqlite.apply_changes(conn_a, changes_b_author)

            # A should have both updates
            cursor = conn_a.execute(
                "SELECT title, author FROM items WHERE source_id = ?", ("shared-item",)
            )
            row = cursor.fetchone()
            assert row[0] == "updated title from A"
            assert row[1] == "updated author from B"

        with get_crdt_connection(config_b) as conn_b:
            crsqlite.apply_changes(conn_b, changes_a_title)

            # B should have both updates
            cursor = conn_b.execute(
                "SELECT title, author FROM items WHERE source_id = ?", ("shared-item",)
            )
            row = cursor.fetchone()
            assert row[0] == "updated title from A"
            assert row[1] == "updated author from B"

    def test_site_id_is_unique_per_database(self, two_crdt_dbs):
        """Each database should have a unique site_id."""
        config_a, config_b = two_crdt_dbs

        with get_crdt_connection(config_a) as conn_a:
            site_id_a = crsqlite.get_site_id(conn_a)

        with get_crdt_connection(config_b) as conn_b:
            site_id_b = crsqlite.get_site_id(conn_b)

        assert site_id_a is not None
        assert site_id_b is not None
        assert site_id_a != site_id_b

    def test_db_version_increments_on_changes(self, crdt_db):
        """Database version should increment with each change."""
        with get_crdt_connection(crdt_db) as conn:
            crsqlite.upgrade_to_crr(conn, "items")

            version_before = crsqlite.get_db_version(conn)

            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "item-1", "content"),
            )
            conn.commit()

            version_after = crsqlite.get_db_version(conn)

            assert version_after > version_before


@crsqlite_required
class TestUpgradeToCRR:
    """Test table upgrade to CRR functionality."""

    def test_upgrade_creates_change_tracking(self, crdt_db):
        """Upgrading table to CRR should enable change tracking."""
        with get_crdt_connection(crdt_db) as conn:
            # Before upgrade, crsql_changes should be empty or not work for this table
            crsqlite.upgrade_to_crr(conn, "items")

            # Insert item
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "item-1", "content"),
            )
            conn.commit()

            # Changes should be tracked
            changes = crsqlite.get_changes_since(conn, since_version=0)
            assert len(changes) > 0

            # At least one change should be for the items table
            tables_changed = {c["table"] for c in changes}
            assert "items" in tables_changed

    def test_upgrade_is_idempotent(self, crdt_db):
        """Upgrading same table twice should not cause errors."""
        with get_crdt_connection(crdt_db) as conn:
            result1 = crsqlite.upgrade_to_crr(conn, "items")
            result2 = crsqlite.upgrade_to_crr(conn, "items")

            # Both should succeed (idempotent)
            assert result1 is True
            assert result2 is True


class TestErrorHandling:
    """Test error handling in sync operations."""

    def test_get_changes_without_crsqlite_returns_empty(self, crdt_db):
        """Getting changes without cr-sqlite loaded should return empty list."""
        # Use regular connection without cr-sqlite
        with get_connection(crdt_db) as conn:
            changes = crsqlite.get_changes_since(conn, since_version=0)
            assert changes == []

    def test_apply_changes_without_crsqlite_returns_zero(self, crdt_db):
        """Applying changes without cr-sqlite loaded should return 0."""
        fake_change = {
            "table": "items",
            "pk": "1",
            "cid": "content",
            "val": "test",
            "col_version": 1,
            "db_version": 1,
            "site_id": "abc123",
            "cl": 1,
            "seq": 0,
        }
        with get_connection(crdt_db) as conn:
            applied = crsqlite.apply_changes(conn, [fake_change])
            assert applied == 0

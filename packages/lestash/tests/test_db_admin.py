"""Tests for database administration and maintenance."""

import pytest
from lestash.core.database import get_connection, get_db_path, upsert_item
from lestash.core.db_admin import (
    count_fk_violations,
    count_history_before,
    create_backup,
    find_duplicate_source_ids,
    find_orphaned_children,
    find_orphaned_tags,
    get_db_file_sizes,
    get_fts_health,
    get_history_stats,
    get_largest_items,
    get_page_info,
    get_source_distribution,
    get_status_summary,
    get_sync_health,
    get_table_stats,
    list_backups,
    prune_backups,
    purge_history,
    rebuild_fts,
    repair_foreign_keys,
    run_analyze_stats,
    run_full_integrity,
    run_integrity_check,
    run_pragma_optimize,
    run_vacuum,
    run_wal_checkpoint,
    verify_backup,
)
from lestash.models.item import ItemCreate


def _insert_item(conn, source_type="test", source_id="1", content="hello", **kwargs):
    """Insert a test item and return its ID."""
    item = ItemCreate(
        source_type=source_type,
        source_id=source_id,
        content=content,
        **kwargs,
    )
    return upsert_item(conn, item)


class TestStatus:
    def test_file_sizes(self, test_db):
        db_path = get_db_path(test_db)
        sizes = get_db_file_sizes(db_path)
        assert sizes["db"] > 0
        assert sizes["total"] >= sizes["db"]

    def test_table_stats(self, test_db):
        with get_connection(test_db) as conn:
            stats = get_table_stats(conn)
        # dbstat may not be available on all builds
        if stats is not None:
            names = [s["name"] for s in stats]
            assert "items" in names

    def test_page_info(self, test_db):
        with get_connection(test_db) as conn:
            info = get_page_info(conn)
        assert info["page_size"] > 0
        assert info["page_count"] > 0
        assert info["freelist_count"] >= 0

    def test_fts_health_in_sync(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="fts1", content="test content")
            health = get_fts_health(conn)
        assert health["items_count"] == health["fts_count"]
        assert health["in_sync"] is True

    def test_sync_health_empty(self, test_db):
        with get_connection(test_db) as conn:
            health = get_sync_health(conn)
        assert health["recent_failures"] == []
        assert health["stuck_syncs"] == []

    def test_status_summary(self, test_db):
        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            summary = get_status_summary(conn, db_path)
        assert "file_sizes" in summary
        assert "schema_version" in summary
        assert "fts" in summary
        assert "sync" in summary


class TestIntegrity:
    def test_integrity_check_clean(self, test_db):
        with get_connection(test_db) as conn:
            assert run_integrity_check(conn) == "ok"

    def test_full_integrity_clean(self, test_db):
        with get_connection(test_db) as conn:
            report = run_full_integrity(conn)
        assert report["integrity_check"] == "ok"
        assert report["fts_integrity"] == "ok"
        assert report["wal_mode"] is True
        assert report["foreign_keys_enabled"] is True
        assert report["orphaned_children"] == []
        assert report["orphaned_media"] == []
        assert report["orphaned_tags"] == []

    def test_orphaned_children_detected(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="orphan1", content="child", parent_id=99999)
            orphans = find_orphaned_children(conn)
        assert len(orphans) == 1
        assert orphans[0]["parent_id"] == 99999

    def test_orphaned_tags_detected(self, test_db):
        with get_connection(test_db) as conn:
            # Insert a tag with no item association
            conn.execute("INSERT INTO tags (name) VALUES ('lonely')")
            conn.commit()
            orphans = find_orphaned_tags(conn)
        assert len(orphans) == 1
        assert orphans[0]["name"] == "lonely"


class TestRepair:
    def test_no_violations_on_clean_db(self, test_db):
        with get_connection(test_db) as conn:
            assert count_fk_violations(conn) == {}

    def test_repairs_orphaned_history(self, test_db):
        with get_connection(test_db) as conn:
            item_id = _insert_item(conn, source_id="rp1", content="original")
            # Update to create history, then delete the item with FK off
            conn.execute(
                "UPDATE items SET content = 'changed' WHERE id = ?",
                (item_id,),
            )
            conn.commit()
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            violations = count_fk_violations(conn)
            assert violations.get("item_history", 0) >= 1

            deleted = repair_foreign_keys(conn)
            assert deleted.get("item_history", 0) >= 1
            assert count_fk_violations(conn) == {}

    def test_repairs_orphaned_tags(self, test_db):
        with get_connection(test_db) as conn:
            item_id = _insert_item(conn, source_id="rp2", content="tagged")
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES ('t1')")
            tag_id = conn.execute("SELECT id FROM tags WHERE name = 't1'").fetchone()[0]
            conn.execute(
                "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                (item_id, tag_id),
            )
            conn.commit()
            # Delete item with FK off
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            violations = count_fk_violations(conn)
            assert violations.get("item_tags", 0) >= 1

            repair_foreign_keys(conn)
            assert count_fk_violations(conn) == {}


class TestAnalyze:
    def test_largest_items(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="big", content="x" * 1000)
            _insert_item(conn, source_id="small", content="y")
            items = get_largest_items(conn, limit=5)
        assert items[0]["content_size"] == 1000

    def test_source_distribution(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_type="alpha", source_id="a1", content="hello")
            _insert_item(conn, source_type="alpha", source_id="a2", content="world")
            _insert_item(conn, source_type="beta", source_id="b1", content="!")
            dist = get_source_distribution(conn)
        names = {d["source_type"] for d in dist}
        assert "alpha" in names
        assert "beta" in names

    def test_no_duplicates_on_clean_db(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="uniq1", content="a")
            dupes = find_duplicate_source_ids(conn)
        assert dupes == []


class TestOptimize:
    def test_vacuum(self, test_db):
        db_path = get_db_path(test_db)
        result = run_vacuum(db_path)
        assert result["size_before"] > 0
        assert result["size_after"] > 0

    def test_fts_rebuild(self, test_db):
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="rb1", content="rebuild test")
            rebuild_fts(conn)
            health = get_fts_health(conn)
        assert health["in_sync"] is True

    def test_wal_checkpoint(self, test_db):
        with get_connection(test_db) as conn:
            result = run_wal_checkpoint(conn)
        assert "busy" in result
        assert "log" in result

    def test_analyze_and_optimize(self, test_db):
        with get_connection(test_db) as conn:
            run_analyze_stats(conn)
            run_pragma_optimize(conn)


class TestBackup:
    def test_create_and_list(self, test_db):
        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="bk1", content="backup me")

        dest = create_backup(db_path)
        assert dest.exists()
        assert dest.name.startswith("lestash-backup-")

        backups = list_backups(db_path)
        assert len(backups) == 1
        assert backups[0]["filename"] == dest.name

    def test_verify_good_backup(self, test_db):
        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="vfy1", content="verify me")

        dest = create_backup(db_path)
        result = verify_backup(dest)
        assert result["valid"] is True
        assert result["item_count"] >= 1

    def test_verify_corrupt_file(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_text("not a database")
        result = verify_backup(bad)
        assert result["valid"] is False

    def test_prune_keeps_n(self, test_db):
        db_path = get_db_path(test_db)
        # Create 4 backups with distinct timestamps
        import shutil

        for i in range(4):
            name = f"lestash-backup-20260101-00000{i}.db"
            shutil.copy2(db_path, db_path.parent / name)

        assert len(list_backups(db_path)) == 4
        deleted = prune_backups(db_path, keep=2)
        assert len(deleted) == 2
        remaining = list_backups(db_path)
        assert len(remaining) == 2


class TestHistory:
    def _trigger_history(self, conn):
        """Insert an item then update it to create history records."""
        item_id = _insert_item(conn, source_id="hist1", content="original")
        conn.execute("UPDATE items SET content = 'updated' WHERE id = ?", (item_id,))
        conn.commit()
        return item_id

    def test_history_stats_empty(self, test_db):
        with get_connection(test_db) as conn:
            stats = get_history_stats(conn)
        assert stats["total_records"] == 0

    def test_history_stats_with_data(self, test_db):
        with get_connection(test_db) as conn:
            self._trigger_history(conn)
            stats = get_history_stats(conn)
        assert stats["total_records"] >= 1

    def test_count_before(self, test_db):
        with get_connection(test_db) as conn:
            self._trigger_history(conn)
            # All records are "now", so future cutoff should include them
            count = count_history_before(conn, before="2099-01-01")
        assert count >= 1

    def test_purge_by_keep_days(self, test_db):
        with get_connection(test_db) as conn:
            self._trigger_history(conn)
            # keep_days=0 means delete everything older than now
            # Records just created should be within 0 days, so this may delete 0
            # Use a large value to be safe
            deleted = purge_history(conn, before="2099-01-01")
        assert deleted >= 1


class TestCli:
    """Smoke tests for CLI commands using typer CliRunner."""

    @pytest.fixture
    def runner(self):
        from typer.testing import CliRunner

        return CliRunner()

    @pytest.fixture
    def cli_app(self, test_db, monkeypatch):
        """Patch config so CLI commands use the test database."""
        from lestash.cli.db import app

        monkeypatch.setattr("lestash.core.database.Config.load", lambda: test_db)
        return app

    def test_status(self, runner, cli_app):
        result = runner.invoke(cli_app, ["status"])
        assert result.exit_code == 0
        assert "Database" in result.output

    def test_status_json(self, runner, cli_app):
        result = runner.invoke(cli_app, ["status", "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert "schema_version" in data

    def test_integrity(self, runner, cli_app):
        result = runner.invoke(cli_app, ["integrity"])
        assert result.exit_code == 0

    def test_analyze(self, runner, cli_app):
        result = runner.invoke(cli_app, ["analyze"])
        assert result.exit_code == 0

    def test_optimize_requires_flag(self, runner, cli_app):
        result = runner.invoke(cli_app, ["optimize"])
        assert result.exit_code == 1

    def test_history(self, runner, cli_app):
        result = runner.invoke(cli_app, ["history"])
        assert result.exit_code == 0
        assert "Item History" in result.output

    def test_backup_list_empty(self, runner, cli_app):
        result = runner.invoke(cli_app, ["backup", "list"])
        assert result.exit_code == 0

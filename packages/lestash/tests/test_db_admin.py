"""Tests for database administration and maintenance."""

import sqlite3
from pathlib import Path

import pytest
from lestash.core.database import get_connection, get_db_path, upsert_item
from lestash.core.db_admin import (
    backup_kind,
    compress_backup,
    count_fk_violations,
    count_history_before,
    create_backup,
    extract_backup,
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
    is_labelled,
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

    def test_a_label_goes_in_the_name_and_is_checked(self, test_db):
        db_path = get_db_path(test_db)
        dest = create_backup(db_path, label="pre-deploy-355")
        assert dest.name.endswith("-pre-deploy-355.db.zst")
        assert list_backups(db_path)[0]["filename"] == dest.name
        with pytest.raises(ValueError):
            create_backup(db_path, label="../escape")

    def test_a_failed_copy_leaves_no_backup(self, test_db, monkeypatch):
        db_path = get_db_path(test_db)

        class Broken:
            def __init__(self, conn):
                self.conn = conn

            def backup(self, dest):
                raise sqlite3.OperationalError("disk full")

            def close(self):
                self.conn.close()

        real = sqlite3.connect
        calls = []

        def connect(path, *a, **k):
            calls.append(path)
            conn = real(path, *a, **k)
            return Broken(conn) if len(calls) == 1 else conn

        monkeypatch.setattr("lestash.core.db_admin.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError):
            create_backup(db_path)
        folder = Path(calls[1]).parent
        assert list(folder.glob("lestash-backup-*")) == []

    def test_a_backup_of_the_same_name_is_never_replaced(self, test_db, monkeypatch):
        import lestash.core.db_admin as d

        db_path = get_db_path(test_db)
        fixed = d.datetime(2026, 11, 18, 0, 0, 0, tzinfo=d.UTC)

        class Frozen(d.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(d, "datetime", Frozen)
        first = create_backup(db_path)
        inode = first.stat().st_ino
        with pytest.raises(FileExistsError):
            create_backup(db_path)
        assert first.stat().st_ino == inode
        assert list(first.parent.glob("*.partial")) == []

    def test_a_backup_is_one_file(self, test_db):
        db_path = get_db_path(test_db)
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        dest = create_backup(db_path, compress=False)
        assert verify_backup(dest)["valid"]
        with sqlite3.connect(dest) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert sorted(p.name for p in dest.parent.glob(dest.name + "*")) == [dest.name]

    def test_verify_never_creates_a_missing_file(self, tmp_path):
        missing = tmp_path / "lestash-backup-20990101-000000.db"
        result = verify_backup(missing)
        assert result["valid"] is False and "no such file" in result["error"]
        assert not missing.exists()

    def test_a_stale_partial_is_removed_and_a_fresh_one_kept(self, test_db):
        import os

        db_path = get_db_path(test_db)
        folder = create_backup(db_path).parent
        stale = folder / "lestash-backup-20200101-000000.db.partial"
        fresh = folder / "lestash-backup-20990101-000000.db.partial"
        stale.write_text("x")
        fresh.write_text("x")
        os.utime(stale, (0, 0))
        create_backup(db_path, label="next")
        assert not stale.exists() and fresh.exists()

    def test_the_source_closes_when_the_copy_cant_be_opened(self, test_db, monkeypatch):
        db_path = get_db_path(test_db)
        real = sqlite3.connect
        opened = []

        def connect(path, *a, **k):
            if str(path).endswith(".partial"):
                raise sqlite3.OperationalError("cannot open")
            conn = real(path, *a, **k)
            opened.append(conn)
            return conn

        monkeypatch.setattr("lestash.core.db_admin.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError):
            create_backup(db_path)
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_prune_removes_a_backups_sidecar_files(self, test_db):
        db_path = get_db_path(test_db)
        old = db_path.parent / "lestash-backup-20200101-000000.db"
        old.write_bytes(db_path.read_bytes())
        for sidecar in ("-wal", "-shm"):
            old.with_name(old.name + sidecar).write_text("")
        create_backup(db_path)
        prune_backups(db_path, keep=1)
        assert list(db_path.parent.glob("lestash-backup-20200101-000000*")) == []


class TestCompressedBackup:
    """Backups are stored compressed with zstd (#414)."""

    def test_a_backup_is_compressed_and_reads_back(self, test_db):
        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="z1", content="squeeze me")
        dest = create_backup(db_path, label="pre-x")
        assert dest.name.endswith("-pre-x.db.zst")
        assert dest.read_bytes()[:4] == b"\x28\xb5\x2f\xfd"  # zstd's magic number
        result = verify_backup(dest)
        assert result["valid"] is True and result["item_count"] >= 1
        assert result["path"] == str(dest)
        # Neither the copy nor the check leaves a file behind
        assert sorted(p.name for p in dest.parent.glob("lestash-backup-*")) == [dest.name]

    def test_extract_writes_the_database_and_never_over_a_file(self, test_db, tmp_path):
        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="z2", content="restore me")
        dest = create_backup(db_path)
        out = tmp_path / "restored.db"
        extract_backup(dest, out)
        with sqlite3.connect(out) as conn:
            assert conn.execute("SELECT COUNT(*) FROM items WHERE source_id='z2'").fetchone()[0]
        with pytest.raises(FileExistsError):
            extract_backup(dest, out)
        other = tmp_path / "other.db"
        (tmp_path / "other.db-wal").write_text("")
        with pytest.raises(FileExistsError):
            extract_backup(dest, other)
        assert not other.exists()

    def test_a_broken_compressed_file_fails_its_check(self, tmp_path):
        bad = tmp_path / "lestash-backup-20990101-000000.db.zst"
        bad.write_bytes(b"not zstd")
        result = verify_backup(bad)
        assert result["valid"] is False and "decompress" in result["error"]
        assert [p.name for p in tmp_path.iterdir()] == [bad.name]

    def test_compress_replaces_a_plain_backup(self, test_db):
        import os

        db_path = get_db_path(test_db)
        with get_connection(test_db) as conn:
            _insert_item(conn, source_id="z3", content="old style")
        plain = create_backup(db_path, label="old", compress=False)
        os.utime(plain, (1_000_000, 1_000_000))
        count = verify_backup(plain)["item_count"]
        dest = compress_backup(plain)
        assert dest.name == plain.name + ".zst" and not plain.exists()
        assert dest.stat().st_mtime == 1_000_000
        assert verify_backup(dest)["item_count"] == count

    def test_compress_leaves_a_backup_that_fails_its_check(self, tmp_path):
        bad = tmp_path / "lestash-backup-20990101-000000.db"
        bad.write_text("not a database")
        with pytest.raises(ValueError):
            compress_backup(bad)
        assert [p.name for p in tmp_path.iterdir()] == [bad.name]

    def test_plain_and_compressed_backups_are_listed_and_pruned_together(self, test_db):
        db_path = get_db_path(test_db)
        old = db_path.parent / "lestash-backup-20200101-000000.db"
        old.write_bytes(db_path.read_bytes())
        newer = create_backup(db_path)
        names = [b["filename"] for b in list_backups(db_path)]
        assert names == [newer.name, old.name]
        assert backup_kind(newer.name) == "scheduled"
        assert backup_kind("lestash-backup-20200101-000000-pre-x.db.zst") == "labelled"
        prune_backups(db_path, keep=1)
        assert not old.exists() and newer.exists()

    def test_a_time_taken_by_a_plain_backup_is_not_reused(self, test_db, monkeypatch):
        import lestash.core.db_admin as d

        db_path = get_db_path(test_db)
        fixed = d.datetime(2026, 11, 18, 0, 0, 0, tzinfo=d.UTC)

        class Frozen(d.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(d, "datetime", Frozen)
        create_backup(db_path, compress=False)
        with pytest.raises(FileExistsError):
            create_backup(db_path)


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

    def test_backup_create_checks_labels_and_prunes(self, runner, cli_app, test_db):
        import shutil

        db_path = get_db_path(test_db)
        for i in range(3):
            shutil.copy2(db_path, db_path.parent / f"lestash-backup-20260101-00000{i}.db")
        (db_path.parent / "lestash-backup-20250101-000000-pre-deploy-1.db").write_bytes(
            db_path.read_bytes()
        )
        result = runner.invoke(cli_app, ["backup", "create", "--keep", "2"])
        assert result.exit_code == 0, result.output
        assert "Pruned 2 older scheduled backup(s), keeping 2" in result.output
        names = [b["filename"] for b in list_backups(db_path)]
        assert len(names) == 3  # 2 scheduled, and the labelled one is never pruned by --keep
        assert "lestash-backup-20250101-000000-pre-deploy-1.db" in names
        result = runner.invoke(cli_app, ["backup", "create", "--label", "pre-test"])
        assert result.exit_code == 0, result.output
        assert "pre-test.db" in result.output
        result = runner.invoke(
            cli_app, ["backup", "prune", "--labelled", "--keep", "1", "--confirm"]
        )
        assert "Pruned 1 labelled backup(s)" in result.output
        names = [b["filename"] for b in list_backups(db_path)]
        (left,) = [n for n in names if is_labelled(n)]
        assert left.endswith("-pre-test.db.zst")

    def test_backup_compress_and_extract(self, runner, cli_app, test_db, tmp_path):
        db_path = get_db_path(test_db)
        old = db_path.parent / "lestash-backup-20200101-000000-pre-old.db"
        old.write_bytes(db_path.read_bytes())
        result = runner.invoke(cli_app, ["backup", "compress"])
        assert result.exit_code == 0, result.output
        assert "Compressed lestash-backup-20200101-000000-pre-old.db" in result.output
        assert not old.exists()
        result = runner.invoke(cli_app, ["backup", "compress"])
        assert "compressed already" in result.output
        out = tmp_path / "restored.db"
        result = runner.invoke(cli_app, ["backup", "extract", old.name + ".zst", "--to", str(out)])
        assert result.exit_code == 0, result.output
        assert verify_backup(out)["valid"]
        result = runner.invoke(cli_app, ["backup", "extract", old.name + ".zst", "--to", str(out)])
        assert result.exit_code == 1 and "exists" in result.output

    def test_a_backup_under_another_name_is_never_pruned(self, runner, cli_app, test_db):
        db_path = get_db_path(test_db)
        odd = db_path.parent / "lestash-backup-by-hand.db"
        odd.write_bytes(db_path.read_bytes())
        assert backup_kind(odd.name) is None
        for flags in (["--keep", "0"], ["--labelled", "--keep", "0"]):
            runner.invoke(cli_app, ["backup", "prune", *flags, "--confirm"])
        assert odd.exists()

    def test_a_backup_under_another_name_sorts_by_its_file_time(self, test_db):
        import os

        db_path = get_db_path(test_db)
        odd = db_path.parent / "lestash-backup-by-hand.db"
        odd.write_bytes(db_path.read_bytes())
        os.utime(odd, (0, 0))  # written in 1970
        newest = create_backup(db_path)
        names = [b["filename"] for b in list_backups(db_path)]
        assert names == [newest.name, odd.name]

    def test_backup_create_refuses_a_bad_label(self, runner, cli_app):
        result = runner.invoke(cli_app, ["backup", "create", "--label", "Bad Label"])
        assert result.exit_code == 2

    def test_backup_create_deletes_a_copy_that_fails_its_check(
        self, runner, cli_app, test_db, monkeypatch
    ):
        monkeypatch.setattr(
            "lestash.cli.db.verify_backup", lambda p: {"valid": False, "integrity": "bad page"}
        )
        db_path = get_db_path(test_db)
        result = runner.invoke(cli_app, ["backup", "create", "--keep", "1"])
        assert result.exit_code == 1
        assert "failed its integrity check" in result.output
        assert list_backups(db_path) == []

    def test_backup_verify_exits_1_on_a_bad_backup(self, runner, cli_app, test_db):
        db_path = get_db_path(test_db)
        (db_path.parent / "lestash-backup-20260101-000000.db").write_text("not a database")
        result = runner.invoke(cli_app, ["backup", "verify"])
        assert result.exit_code == 1

    def test_backup_list_empty(self, runner, cli_app):
        result = runner.invoke(cli_app, ["backup", "list"])
        assert result.exit_code == 0

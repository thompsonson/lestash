"""Database administration and maintenance for Le Stash.

Pure logic layer — no Rich/Typer dependencies. All functions take a
sqlite3.Connection (or Path) and return plain dicts/lists for the CLI
and API layers to format.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_ALL_TABLES = [
    "items",
    "item_history",
    "item_tags",
    "tags",
    "sources",
    "sync_log",
    "log_entries",
    "person_profiles",
    "post_cache",
    "collections",
    "collection_items",
    "item_media",
]


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0


def _format_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def get_db_file_sizes(db_path: Path) -> dict:
    """Return byte sizes for the database, WAL, and SHM files."""
    sizes: dict[str, int] = {}
    for suffix, key in [("", "db"), ("-wal", "wal"), ("-shm", "shm")]:
        p = db_path.parent / (db_path.name + suffix) if suffix else db_path
        sizes[key] = p.stat().st_size if p.exists() else 0
    sizes["total"] = sum(sizes.values())
    return sizes


def get_table_stats(conn: sqlite3.Connection) -> list[dict] | None:
    """Row counts and byte sizes per table via dbstat. Returns None if dbstat unavailable."""
    try:
        size_rows = conn.execute(
            "SELECT name, SUM(pgsize) as size FROM dbstat GROUP BY name ORDER BY size DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return None

    size_map = {row["name"]: row["size"] for row in size_rows}
    results = []
    for table in _ALL_TABLES:
        if table in size_map or _safe_count(conn, table) > 0:
            results.append(
                {
                    "name": table,
                    "rows": _safe_count(conn, table),
                    "size_bytes": size_map.get(table, 0),
                }
            )
    return results


def get_index_stats(conn: sqlite3.Connection) -> list[dict] | None:
    """Byte sizes for all indexes via dbstat. Returns None if unavailable."""
    try:
        rows = conn.execute(
            """SELECT name, SUM(pgsize) as size FROM dbstat
               WHERE name LIKE 'idx_%' OR name LIKE 'sqlite_autoindex%'
               GROUP BY name ORDER BY size DESC"""
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return [{"name": r["name"], "size_bytes": r["size"]} for r in rows]


def get_page_info(conn: sqlite3.Connection) -> dict:
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "freelist_bytes": freelist * page_size,
    }


def get_fts_health(conn: sqlite3.Connection) -> dict:
    items_count = _safe_count(conn, "items")
    try:
        # Use the docsize shadow table for reliable counting
        fts_count = conn.execute("SELECT COUNT(*) FROM items_fts_docsize").fetchone()[0]
    except sqlite3.OperationalError:
        fts_count = -1
    return {
        "items_count": items_count,
        "fts_count": fts_count,
        "in_sync": items_count == fts_count,
    }


def get_sync_health(conn: sqlite3.Connection) -> dict:
    recent_failures = [
        dict(r)
        for r in conn.execute(
            """SELECT source_type, started_at, error_message
               FROM sync_log WHERE status = 'failed'
               ORDER BY started_at DESC LIMIT 5"""
        ).fetchall()
    ]
    stuck_syncs = [
        dict(r)
        for r in conn.execute(
            """SELECT source_type, started_at
               FROM sync_log WHERE status = 'running'
               AND started_at < datetime('now', '-1 hour')"""
        ).fetchall()
    ]
    return {"recent_failures": recent_failures, "stuck_syncs": stuck_syncs}


def get_embedding_coverage(conn: sqlite3.Connection) -> dict | None:
    """Embedding stats, or None if sqlite-vec is unavailable."""
    try:
        from lestash.core.embeddings import (
            ensure_vec_table,
            get_embedding_stats,
            load_vec_extension,
        )

        load_vec_extension(conn)
        ensure_vec_table(conn)
        return get_embedding_stats(conn)
    except Exception:
        return None


def get_status_summary(conn: sqlite3.Connection, db_path: Path) -> dict:
    """Aggregate dashboard data."""
    from lestash.core.database import get_schema_version

    return {
        "db_path": str(db_path),
        "file_sizes": get_db_file_sizes(db_path),
        "schema_version": get_schema_version(conn),
        "page_info": get_page_info(conn),
        "tables": get_table_stats(conn),
        "indexes": get_index_stats(conn),
        "fts": get_fts_health(conn),
        "embeddings": get_embedding_coverage(conn),
        "sync": get_sync_health(conn),
    }


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #


def run_integrity_check(conn: sqlite3.Connection) -> str:
    """PRAGMA integrity_check. Returns 'ok' or error details."""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    results = [r[0] for r in rows]
    return results[0] if len(results) == 1 else "\n".join(results)


def run_foreign_key_check(conn: sqlite3.Connection) -> list[dict]:
    """PRAGMA foreign_key_check. Returns list of violations."""
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return [{"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]} for r in rows]


def run_fts_integrity_check(conn: sqlite3.Connection) -> str:
    """FTS5 integrity-check. Returns 'ok' or error message."""
    try:
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('integrity-check')")
        return "ok"
    except sqlite3.OperationalError as e:
        return str(e)


def check_wal_mode(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def check_foreign_keys_enabled(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def find_orphaned_children(conn: sqlite3.Connection) -> list[dict]:
    """Items whose parent_id points to a non-existent item."""
    rows = conn.execute(
        """SELECT i.id, i.source_type, i.parent_id
           FROM items i LEFT JOIN items p ON i.parent_id = p.id
           WHERE i.parent_id IS NOT NULL AND p.id IS NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_orphaned_media(conn: sqlite3.Connection) -> list[dict]:
    """Media rows whose item_id doesn't exist."""
    rows = conn.execute(
        """SELECT m.id, m.item_id, m.media_type
           FROM item_media m LEFT JOIN items i ON m.item_id = i.id
           WHERE i.id IS NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_orphaned_tags(conn: sqlite3.Connection) -> list[dict]:
    """Tags with no item_tags entries."""
    rows = conn.execute(
        """SELECT t.id, t.name
           FROM tags t LEFT JOIN item_tags it ON t.id = it.tag_id
           WHERE it.tag_id IS NULL"""
    ).fetchall()
    return [dict(r) for r in rows]


def run_full_integrity(conn: sqlite3.Connection) -> dict:
    """Run all integrity checks and return a combined report."""
    return {
        "integrity_check": run_integrity_check(conn),
        "foreign_key_check": run_foreign_key_check(conn),
        "fts_integrity": run_fts_integrity_check(conn),
        "wal_mode": check_wal_mode(conn),
        "foreign_keys_enabled": check_foreign_keys_enabled(conn),
        "orphaned_children": find_orphaned_children(conn),
        "orphaned_media": find_orphaned_media(conn),
        "orphaned_tags": find_orphaned_tags(conn),
    }


# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #


def get_largest_items(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """SELECT id, source_type, title, LENGTH(content) as content_size
           FROM items ORDER BY content_size DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_source_distribution(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT source_type,
                  COUNT(*) as count,
                  SUM(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) as parents,
                  SUM(CASE WHEN parent_id IS NOT NULL THEN 1 ELSE 0 END) as children,
                  SUM(LENGTH(content)) as total_content_size
           FROM items GROUP BY source_type ORDER BY count DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_duplicate_source_ids(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT source_type, source_id, COUNT(*) as cnt
           FROM items WHERE source_id IS NOT NULL
           GROUP BY source_type, source_id HAVING cnt > 1"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_embedding_coverage_by_source(conn: sqlite3.Connection) -> list[dict] | None:
    """Embedding coverage per source type. None if vec unavailable."""
    try:
        from lestash.core.embeddings import load_vec_extension

        load_vec_extension(conn)
        rows = conn.execute(
            """SELECT i.source_type,
                      COUNT(*) as total,
                      COUNT(v.rowid) as embedded
               FROM items i
               LEFT JOIN vec_items v ON i.id = v.item_id
               WHERE i.parent_id IS NULL
               GROUP BY i.source_type ORDER BY total DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return None


def run_full_analysis(conn: sqlite3.Connection) -> dict:
    return {
        "largest_items": get_largest_items(conn),
        "source_distribution": get_source_distribution(conn),
        "duplicates": find_duplicate_source_ids(conn),
        "fts": get_fts_health(conn),
        "orphaned_children": find_orphaned_children(conn),
        "orphaned_media": find_orphaned_media(conn),
        "orphaned_tags": find_orphaned_tags(conn),
        "embedding_coverage_by_source": get_embedding_coverage_by_source(conn),
    }


# --------------------------------------------------------------------------- #
# Repair
# --------------------------------------------------------------------------- #

_FK_RELATIONSHIPS = [
    ("item_history", "item_id", "items"),
    ("item_tags", "item_id", "items"),
    ("collection_items", "item_id", "items"),
    ("item_media", "item_id", "items"),
]


def count_fk_violations(conn: sqlite3.Connection) -> dict[str, int]:
    """Count orphaned rows per child table (dry-run)."""
    counts: dict[str, int] = {}
    for child, fk_col, parent in _FK_RELATIONSHIPS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM [{child}] WHERE [{fk_col}] NOT IN (SELECT id FROM [{parent}])"
        ).fetchone()[0]
        if n:
            counts[child] = n
    return counts


def repair_foreign_keys(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete rows that violate FK constraints. Returns deleted counts per table."""
    results: dict[str, int] = {}
    for child, fk_col, parent in _FK_RELATIONSHIPS:
        cursor = conn.execute(
            f"DELETE FROM [{child}] WHERE [{fk_col}] NOT IN (SELECT id FROM [{parent}])"
        )
        if cursor.rowcount:
            results[child] = cursor.rowcount
    conn.commit()
    return results


# --------------------------------------------------------------------------- #
# Optimize
# --------------------------------------------------------------------------- #


def run_vacuum(db_path: Path) -> dict:
    """VACUUM the database. Uses a raw connection (cannot run inside get_connection).

    Returns dict with size_before, size_after, freed.
    """
    size_before = db_path.stat().st_size
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    size_after = db_path.stat().st_size
    return {
        "size_before": size_before,
        "size_after": size_after,
        "freed": size_before - size_after,
    }


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index."""
    conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    conn.commit()


def run_wal_checkpoint(conn: sqlite3.Connection) -> dict:
    """Checkpoint and truncate the WAL file."""
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {"busy": row[0], "log": row[1], "checkpointed": row[2]}


def run_pragma_optimize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA optimize")


def run_analyze_stats(conn: sqlite3.Connection) -> None:
    conn.execute("ANALYZE")


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

BACKUP_PREFIX = "lestash-backup-"
BACKUP_GLOB = f"{BACKUP_PREFIX}*.db"


def _backup_dir(db_path: Path, backup_dir: Path | None = None) -> Path:
    d = backup_dir or db_path.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_backup(db_path: Path, backup_dir: Path | None = None) -> Path:
    """Create a timestamped backup using SQLite's online backup API."""
    dest_dir = _backup_dir(db_path, backup_dir)
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    dest_path = dest_dir / f"{BACKUP_PREFIX}{ts}.db"

    source_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    logger.info(f"Backup created: {dest_path} ({dest_path.stat().st_size} bytes)")
    return dest_path


def list_backups(db_path: Path, backup_dir: Path | None = None) -> list[dict]:
    """List managed backups sorted newest-first."""
    dest_dir = _backup_dir(db_path, backup_dir)
    backups = []
    for p in sorted(dest_dir.glob(BACKUP_GLOB), reverse=True):
        backups.append(
            {
                "path": str(p),
                "filename": p.name,
                "size_bytes": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(),
            }
        )
    return backups


def verify_backup(backup_path: Path) -> dict:
    """Verify a backup file's integrity."""
    result: dict = {"path": str(backup_path), "valid": False}
    try:
        conn = sqlite3.connect(backup_path)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            integrity = rows[0][0] if len(rows) == 1 else "\n".join(r[0] for r in rows)
            result["integrity"] = integrity
            result["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            result["item_count"] = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            result["valid"] = integrity == "ok"
        finally:
            conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def prune_backups(db_path: Path, keep: int = 5, backup_dir: Path | None = None) -> list[Path]:
    """Delete oldest managed backups beyond the keep count. Returns deleted paths."""
    backups = list_backups(db_path, backup_dir)
    to_delete = backups[keep:]  # already sorted newest-first
    deleted = []
    for b in to_delete:
        p = Path(b["path"])
        p.unlink()
        deleted.append(p)
        logger.info(f"Deleted backup: {p}")
    return deleted


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def get_history_stats(conn: sqlite3.Connection) -> dict:
    total = _safe_count(conn, "item_history")

    by_type = [
        dict(r)
        for r in conn.execute(
            """SELECT change_type, COUNT(*) as count
               FROM item_history GROUP BY change_type"""
        ).fetchall()
    ]

    date_range = conn.execute(
        "SELECT MIN(changed_at) as earliest, MAX(changed_at) as latest FROM item_history"
    ).fetchone()

    # Approximate size via dbstat
    try:
        size = conn.execute(
            "SELECT SUM(pgsize) FROM dbstat WHERE name = 'item_history'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        size = None

    return {
        "total_records": total,
        "by_type": by_type,
        "earliest": date_range["earliest"] if date_range else None,
        "latest": date_range["latest"] if date_range else None,
        "size_bytes": size,
    }


def count_history_before(
    conn: sqlite3.Connection,
    *,
    before: str | None = None,
    keep_days: int | None = None,
) -> int:
    """Count history records that would be purged (dry-run)."""
    if keep_days is not None:
        return conn.execute(
            "SELECT COUNT(*) FROM item_history WHERE changed_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        ).fetchone()[0]
    if before is not None:
        return conn.execute(
            "SELECT COUNT(*) FROM item_history WHERE changed_at < ?",
            (before,),
        ).fetchone()[0]
    return 0


def purge_history(
    conn: sqlite3.Connection,
    *,
    before: str | None = None,
    keep_days: int | None = None,
) -> int:
    """Delete old item_history records. Returns count deleted."""
    if keep_days is not None:
        cursor = conn.execute(
            "DELETE FROM item_history WHERE changed_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
    elif before is not None:
        cursor = conn.execute(
            "DELETE FROM item_history WHERE changed_at < ?",
            (before,),
        )
    else:
        return 0
    conn.commit()
    return cursor.rowcount

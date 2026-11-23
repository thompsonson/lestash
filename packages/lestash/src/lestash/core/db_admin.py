"""Database administration and maintenance for Le Stash.

Pure logic layer — no Rich/Typer dependencies. All functions take a
sqlite3.Connection (or Path) and return plain dicts/lists for the CLI
and API layers to format.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, cast

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
    "people",
    "person_identities",
    "item_agents",
    "item_mentions",
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
    """Coverage by today's model, per source type, of the items that should have a
    vector: the same rule as ``get_embedding_stats`` (#308). None if it can't be read."""
    try:
        from lestash.core.embed_units import TITLE_ONLY_SLACK
        from lestash.core.embeddings import MODEL_NAME, WANTED

        rows = conn.execute(
            f"""SELECT i.source_type,
                       COUNT(*) as total,
                       COUNT(m.item_id) as embedded
                FROM items i
                LEFT JOIN embedding_meta m ON m.item_id = i.id AND m.model = ?
                WHERE {WANTED}
                GROUP BY i.source_type ORDER BY total DESC""",
            (MODEL_NAME, TITLE_ONLY_SLACK),
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
BACKUP_GLOB = f"{BACKUP_PREFIX}*.db"  # an uncompressed copy, which SQLite can open
ZSTD_SUFFIX = ".zst"
# A backup is stored compressed (#414): 1.34 GB became 0.41 GB. Older ones are plain .db
BACKUP_GLOBS = (BACKUP_GLOB, f"{BACKUP_GLOB}{ZSTD_SUFFIX}")
_ZSTD_LEVEL = 3  # 2.6 s for 1.34 GB; higher levels save little more


def _backup_dir(db_path: Path, backup_dir: Path | None = None) -> Path:
    """``paths.backups_dir()`` for the default database; beside any other (#304)."""
    from lestash.core import paths

    if backup_dir is None and db_path.resolve() == paths.database_path().resolve():
        backup_dir = paths.backups_dir()
    d = backup_dir or db_path.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")


PARTIAL_SUFFIX = ".partial"
_STALE_PARTIAL_SECONDS = 3600  # a copy takes seconds; an hour-old one was cut short


def _remove_stale_partials(dest_dir: Path) -> None:
    """Remove copies a killed process left half-made. A copy still being made is newer."""
    cutoff = datetime.now(tz=UTC).timestamp() - _STALE_PARTIAL_SECONDS
    for p in dest_dir.glob(f"{BACKUP_PREFIX}*{PARTIAL_SUFFIX}"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                logger.info(f"Removed a half-made backup: {p}")
        except FileNotFoundError:
            pass


def is_compressed(p: Path) -> bool:
    return p.name.endswith(ZSTD_SUFFIX)


def _compress(src: Path, dest: Path) -> None:
    """Compress ``src`` into ``dest`` (made new), then flush it to disk."""
    import zstandard

    # The checksum makes a damaged file fail to decompress, rather than give wrong bytes.
    # The size in the frame's header shows a cut-short file: it decompresses short.
    cctx = zstandard.ZstdCompressor(level=_ZSTD_LEVEL, threads=-1, write_checksum=True)
    with src.open("rb") as fin, dest.open("xb") as fout:
        cctx.copy_stream(fin, fout, size=os.fstat(fin.fileno()).st_size)
        fout.flush()
        os.fsync(fout.fileno())


class BackupUnreadable(OSError):
    """The backup itself could not be read: a fault in the backup, not in the check."""


class _Reader:
    """A file whose open and read failures are BackupUnreadable, so they are told apart
    from failures to write the copy."""

    def __init__(self, src: Path) -> None:
        self.name = src.name
        try:
            self.f = src.open("rb")
        except OSError as e:
            raise BackupUnreadable(e.errno, f"cannot read {self.name}: {e.strerror}") from e

    def read(self, n: int = -1) -> bytes:
        try:
            return self.f.read(n)
        except OSError as e:
            raise BackupUnreadable(e.errno, f"cannot read {self.name}: {e.strerror}") from e

    def close(self) -> None:
        self.f.close()


def _partial_for(dest: Path) -> Path:
    """A temporary name beside ``dest``, its own, so two runs at once never share one.

    One a killed run leaves in a backup folder is a stale .partial, which a later
    create removes; one left beside an ``extract`` destination is not, and stays.
    """
    return dest.with_name(f"{dest.name}.{os.getpid()}-{os.urandom(4).hex()}{PARTIAL_SUFFIX}")


def _frame_end(src: Path, header: bytes) -> int:
    """Where the zstd frame starting ``src`` ends, found by walking its block headers.

    So data after the frame, or a frame cut short, shows against the file's size
    without decompressing. Raises ZstdError for a reserved block type.
    """
    import zstandard

    pos = zstandard.frame_header_size(header)
    has_checksum = bool(header[4] & 0x04)  # the frame header descriptor's checksum flag
    reader = _Reader(src)
    try:
        f = reader.f
        while True:
            f.seek(pos)
            block = reader.read(3)
            if len(block) < 3:
                return pos + len(block) + 1  # cut short inside a block header: past the end
            value = int.from_bytes(block, "little")
            last, kind, size = value & 1, (value >> 1) & 3, value >> 3
            if kind == 3:
                raise zstandard.ZstdError("a reserved block type: not a zstd backup")
            pos += 3 + (1 if kind == 1 else size)  # an RLE block stores one byte
            if last:
                return pos + (4 if has_checksum else 0)
    finally:
        reader.close()


def _decompress(src: Path, dest: Path) -> None:
    """Decompress ``src`` into ``dest`` (made new), then flush it to disk.

    Raises ZstdError for a file that is damaged, cut short, or has data after its one
    frame; BackupUnreadable if ``src`` can't be read.
    """
    import zstandard

    head = _Reader(src)
    try:
        header = head.read(18)  # a zstd frame header is at most 18 bytes
    finally:
        head.close()
    expected = zstandard.get_frame_parameters(header).content_size
    if expected == zstandard.CONTENTSIZE_UNKNOWN:
        raise zstandard.ZstdError("the backup's frame does not record its size")
    end, size = _frame_end(src, header), src.stat().st_size
    if end > size:
        raise zstandard.ZstdError("the backup ends before its frame does: truncated")
    if end < size:
        raise zstandard.ZstdError(f"{size - end:,} bytes after the end of the backup's frame")
    reader = _Reader(src)
    written = 0
    try:
        with dest.open("xb") as fout:
            dctx = zstandard.ZstdDecompressor()
            # It only calls read(), which _Reader has
            source = cast("IO[bytes]", reader)
            with dctx.stream_reader(source, read_across_frames=False, closefd=False) as r:
                # Bounded: at most 1 MiB of output in memory at a time
                while chunk := r.read(1 << 20):
                    fout.write(chunk)
                    written += len(chunk)
            # A cut-short file ends early without an error: its size shows it
            if written != expected:
                raise zstandard.ZstdError(
                    f"the backup decompresses to {written:,} of {expected:,} bytes: truncated"
                )
            fout.flush()
            os.fsync(fout.fileno())
    finally:
        reader.close()


def _link_into_place(partial: Path, dest: Path) -> None:
    """Give a finished file its name. A hard link fails if the name was taken
    meanwhile, where rename would replace it."""
    os.link(partial, dest)


def create_backup(
    db_path: Path,
    backup_dir: Path | None = None,
    label: str | None = None,
    compress: bool = True,
) -> Path:
    """Create a timestamped backup using SQLite's online backup API.

    The copy is consistent while the database is in use, and is one file: its journal
    mode is set to DELETE, so no ``-wal`` or ``-shm`` sits beside it. It is written under
    a temporary name and moved into place when complete, so a copy in progress never
    counts as a backup. ``label`` (lower case, digits, hyphens) says why, e.g.
    ``pre-deploy-355``. Raises FileExistsError rather than replace a backup of the
    same name, made in the same second.

    The copy is stored compressed with zstd, as ``….db.zst`` (#414), unless ``compress``
    is False. Check it with ``verify_backup``, which reads it back.
    """
    if label is not None and not _LABEL.match(label):
        raise ValueError(f"label must be lower case letters, digits and hyphens: {label!r}")
    dest_dir = _backup_dir(db_path, backup_dir)
    for d in _backup_dirs(db_path, backup_dir):  # checks run in each folder backups are in
        _remove_stale_partials(d)
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    plain_name = f"{BACKUP_PREFIX}{ts}{'-' + label if label else ''}.db"
    dest_path = dest_dir / (plain_name + (ZSTD_SUFFIX if compress else ""))
    # One time, one backup: neither form of the name may be taken
    for name in (plain_name, plain_name + ZSTD_SUFFIX):
        if (dest_dir / name).exists():
            raise FileExistsError(f"a backup named {name} exists already")
    # Names of their own, so a second create in the same second can't remove these
    partial = _partial_for(dest_dir / plain_name)
    zst_partial = _partial_for(dest_dir / (plain_name + ZSTD_SUFFIX))

    try:
        # The source closes even when the copy's file can't be opened.
        with (
            closing(sqlite3.connect(db_path)) as source_conn,
            closing(sqlite3.connect(partial)) as dest_conn,
        ):
            source_conn.backup(dest_conn)
            dest_conn.execute("PRAGMA journal_mode = DELETE")
        if compress:
            _compress(partial, zst_partial)
            _link_into_place(zst_partial, dest_path)
        else:
            _link_into_place(partial, dest_path)
    finally:
        partial.unlink(missing_ok=True)
        zst_partial.unlink(missing_ok=True)

    logger.info(f"Backup created: {dest_path} ({dest_path.stat().st_size} bytes)")
    return dest_path


def _backup_dirs(db_path: Path, backup_dir: Path | None = None) -> list[Path]:
    """Where backups are looked for: the backup folder, and beside the database too,
    where backups of the default database were made before #304."""
    dest_dir = _backup_dir(db_path, backup_dir)
    if backup_dir is None and dest_dir != db_path.parent and db_path.parent.is_dir():
        return [dest_dir, db_path.parent]
    return [dest_dir]


def find_backup(db_path: Path, filename: str) -> Path:
    """A managed backup by its file name; in the backup folder if none is found."""
    for d in _backup_dirs(db_path):
        if (d / filename).is_file():
            return d / filename
    return _backup_dir(db_path) / filename


def _backup_time(p: Path) -> str:
    """When a backup was made, as ``YYYYMMDD-HHMMSS`` UTC: from its name, else its file time.

    So a file under another name sorts by when it was written, not above every backup.
    """
    if backup_kind(p.name) is not None:
        return p.name[len(BACKUP_PREFIX) : len(BACKUP_PREFIX) + 15]
    return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).strftime("%Y%m%d-%H%M%S")


def list_backups(db_path: Path, backup_dir: Path | None = None) -> list[dict]:
    """List managed backups sorted newest-first."""
    found = [p for d in _backup_dirs(db_path, backup_dir) for g in BACKUP_GLOBS for p in d.glob(g)]
    backups = []
    for p in sorted(found, key=_backup_time, reverse=True):
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
    """Verify a backup file's integrity.

    A compressed backup is read back into a temporary file beside it, checked, and the
    file removed: so a pass also shows it can be restored.
    """
    result: dict = {"path": str(backup_path), "valid": False}
    if not backup_path.is_file():
        result["error"] = f"no such file: {backup_path}"
        return result
    if is_compressed(backup_path):
        return _verify_compressed(backup_path)
    try:
        # Read-only: checking a backup never changes it, or creates a file.
        conn = sqlite3.connect(f"{backup_path.resolve().as_uri()}?mode=ro", uri=True)
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


def _verify_compressed(backup_path: Path) -> dict:
    """Check a zstd backup by reading it back into a temporary file beside it.

    A file that won't decompress is invalid. One that can't be written out, say for want
    of space, is ``unchecked``: it says nothing about the backup, so it is not invalid.
    """
    import zstandard

    result: dict = {"path": str(backup_path), "valid": False}
    plain = _partial_for(backup_path.with_name(backup_path.name + ".verify"))
    try:
        _decompress(backup_path, plain)
    except BackupUnreadable as e:
        plain.unlink(missing_ok=True)
        result["error"] = str(e)
        return result
    except zstandard.ZstdError as e:
        plain.unlink(missing_ok=True)
        result["error"] = f"cannot decompress: {e}"
        return result
    except OSError as e:
        plain.unlink(missing_ok=True)
        result["unchecked"] = True
        result["error"] = f"could not check it: {e}"
        return result
    try:
        checked = verify_backup(plain)
    finally:
        plain.unlink(missing_ok=True)
    return {**checked, "path": str(backup_path)}


def _has_wal(p: Path) -> bool:
    """A plain backup with a non-empty ``-wal``, or a ``-journal``, beside it: SQLite
    reads that file as part of it, and a plain copy would leave it out."""
    wal = p.with_name(p.name + "-wal")
    journal = p.with_name(p.name + "-journal")
    return (wal.is_file() and wal.stat().st_size > 0) or journal.is_file()


_SCHEDULED = re.compile(rf"^{BACKUP_PREFIX}\d{{8}}-\d{{6}}\.db(\.zst)?$")
_LABELLED = re.compile(rf"^{BACKUP_PREFIX}\d{{8}}-\d{{6}}-[a-z0-9][a-z0-9-]*\.db(\.zst)?$")


def backup_kind(filename: str) -> str | None:
    """``scheduled`` (no label), ``labelled`` (made before a change), or None.

    None is a file under some other name, such as one made by hand: never pruned.
    """
    if _SCHEDULED.match(filename):
        return "scheduled"
    if _LABELLED.match(filename):
        return "labelled"
    return None


def is_labelled(filename: str) -> bool:
    """A labelled backup was made before a change, under ``create --label``."""
    return backup_kind(filename) == "labelled"


def prune_backups(
    db_path: Path, keep: int = 5, backup_dir: Path | None = None, labelled: bool = False
) -> list[Path]:
    """Delete the oldest backups of one kind beyond the keep count. Returns deleted paths.

    Scheduled (unlabelled) and labelled backups are counted apart, so the schedule's
    pruning never deletes a copy made before a change. A file under any other name is
    never pruned.
    """
    kind = "labelled" if labelled else "scheduled"
    backups = [b for b in list_backups(db_path, backup_dir) if backup_kind(b["filename"]) == kind]
    to_delete = backups[keep:]  # already sorted newest-first
    deleted = []
    for b in to_delete:
        p = Path(b["path"])
        p.unlink()
        for sidecar in ("-wal", "-shm", "-journal"):  # left by copies made in WAL mode
            p.with_name(p.name + sidecar).unlink(missing_ok=True)
        deleted.append(p)
        logger.info(f"Deleted backup: {p}")
    return deleted


def extract_backup(backup_path: Path, dest: Path) -> Path:
    """Write the database a backup holds to ``dest``, decompressing a ``.zst`` one.

    For a restore. Never writes over a file, or beside a ``-wal``, ``-shm`` or ``-journal`` one,
    which SQLite would read as part of it.
    """
    if not backup_path.is_file():
        raise FileNotFoundError(f"no such file: {backup_path}")
    if _has_wal(backup_path):
        raise ValueError(
            f"{backup_path.name} has a -wal or -journal beside it; open it with SQLite instead"
        )
    # SQLite reads a -wal, -shm or hot -journal beside a database as part of it
    for p in [dest] + [dest.with_name(dest.name + s) for s in ("-wal", "-shm", "-journal")]:
        if p.exists():
            raise FileExistsError(f"{p} exists; move it aside first")
    partial = _partial_for(dest)
    try:
        if is_compressed(backup_path):
            _decompress(backup_path, partial)
        else:
            shutil.copyfile(backup_path, partial)
        _link_into_place(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
    return dest


def compress_backup(backup_path: Path) -> Path:
    """Compress a plain ``.db`` backup, keeping its name and time (#414).

    The original is checked first and deleted only once the compressed copy reads back
    whole, so a failure leaves it as it was. Raises ValueError if it fails its check.
    """
    if is_compressed(backup_path):
        return backup_path
    if _has_wal(backup_path):
        raise ValueError(f"{backup_path.name} has a -wal or -journal beside it, left as it is")
    before = verify_backup(backup_path)
    if not before["valid"]:
        raise ValueError(
            f"{backup_path.name} fails its check, left as it is: "
            f"{before.get('error', before.get('integrity'))}"
        )
    dest = backup_path.with_name(backup_path.name + ZSTD_SUFFIX)
    if dest.exists():
        # A run cut short after naming the compressed copy: finish it if that copy
        # holds the same database, else leave both
        done = verify_backup(dest)
        if not (done["valid"] and done["item_count"] == before["item_count"]):
            raise FileExistsError(f"a backup named {dest.name} exists already")
        _remove_plain(backup_path)
        return dest
    partial = _partial_for(dest)
    try:
        _compress(backup_path, partial)
        # Checked before it takes its name, so a cut-short run leaves only a .partial
        after = _verify_compressed(partial)
        if not after["valid"] or after["item_count"] != before["item_count"]:
            raise ValueError(
                f"the compressed copy of {backup_path.name} did not read back "
                f"({after.get('error', after.get('integrity'))}); the original is kept"
            )
        _link_into_place(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
    # Its old time once it has its name: an old .partial would look stale to a create
    st = backup_path.stat()
    os.utime(dest, (st.st_atime, st.st_mtime))
    _remove_plain(backup_path)
    return dest


def _remove_plain(backup_path: Path) -> None:
    """Delete a plain backup, and any empty sidecar SQLite left beside it."""
    backup_path.unlink()
    for sidecar in ("-wal", "-shm", "-journal"):
        backup_path.with_name(backup_path.name + sidecar).unlink(missing_ok=True)


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

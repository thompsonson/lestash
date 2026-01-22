"""cr-sqlite extension management for Le Stash.

This module handles downloading, loading, and using the cr-sqlite extension
for CRDT-based multi-master replication.

cr-sqlite converts regular SQLite tables into Conflict-free Replicated Relations (CRRs)
that can be synced across multiple devices without conflicts.

References:
- https://github.com/vlcn-io/cr-sqlite
- https://vlcn.io/docs/cr-sqlite/intro
"""

import json
import logging
import platform
import sqlite3
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

# cr-sqlite version to use
CRSQLITE_VERSION = "0.16.3"

# GitHub release URL pattern
RELEASE_URL = (
    f"https://github.com/vlcn-io/cr-sqlite/releases/download/v{CRSQLITE_VERSION}"
)

# Platform-specific extension mappings
PLATFORM_MAP = {
    ("Darwin", "x86_64"): ("crsqlite-darwin-x86_64.zip", "crsqlite.dylib"),
    ("Darwin", "arm64"): ("crsqlite-darwin-aarch64.zip", "crsqlite.dylib"),
    ("Linux", "x86_64"): ("crsqlite-linux-x86_64.zip", "crsqlite.so"),
    ("Linux", "aarch64"): ("crsqlite-linux-aarch64.zip", "crsqlite.so"),
    ("Windows", "AMD64"): ("crsqlite-windows-x86_64.zip", "crsqlite.dll"),
}


def get_extension_dir() -> Path:
    """Get the directory where cr-sqlite extensions are stored."""
    # Store in the lestash config directory
    ext_dir = Path.home() / ".config" / "lestash" / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    return ext_dir


def get_platform_info() -> tuple[str, str]:
    """Get current platform and architecture."""
    system = platform.system()
    machine = platform.machine()

    # Normalize architecture names
    if machine in ("x86_64", "AMD64"):
        machine = "x86_64" if system != "Windows" else "AMD64"
    elif machine in ("arm64", "aarch64"):
        machine = "arm64" if system == "Darwin" else "aarch64"

    return system, machine


def get_extension_path() -> Path | None:
    """Get the path to the cr-sqlite extension for the current platform.

    Returns:
        Path to the extension file, or None if platform is not supported.
    """
    system, machine = get_platform_info()
    platform_key = (system, machine)

    if platform_key not in PLATFORM_MAP:
        logger.warning(f"Unsupported platform: {system}/{machine}")
        return None

    _, ext_name = PLATFORM_MAP[platform_key]
    ext_path = get_extension_dir() / ext_name

    return ext_path


def download_extension(force: bool = False) -> Path | None:
    """Download the cr-sqlite extension for the current platform.

    Args:
        force: Re-download even if extension already exists.

    Returns:
        Path to the downloaded extension, or None if failed.
    """
    system, machine = get_platform_info()
    platform_key = (system, machine)

    if platform_key not in PLATFORM_MAP:
        logger.error(f"Unsupported platform: {system}/{machine}")
        return None

    zip_name, ext_name = PLATFORM_MAP[platform_key]
    ext_path = get_extension_dir() / ext_name

    if ext_path.exists() and not force:
        logger.debug(f"cr-sqlite extension already exists: {ext_path}")
        return ext_path

    url = f"{RELEASE_URL}/{zip_name}"
    logger.info(f"Downloading cr-sqlite from {url}")

    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            zip_data = BytesIO(response.read())

        with zipfile.ZipFile(zip_data, "r") as zf:
            # Find the extension file in the archive
            for name in zf.namelist():
                if name.endswith(ext_name) or name == ext_name:
                    # Extract to extension directory
                    content = zf.read(name)
                    ext_path.write_bytes(content)
                    # Make executable on Unix
                    if system != "Windows":
                        ext_path.chmod(0o755)
                    logger.info(f"Downloaded cr-sqlite to {ext_path}")
                    return ext_path

        logger.error(f"Extension file not found in archive: {zip_name}")
        return None

    except Exception as e:
        logger.error(f"Failed to download cr-sqlite: {e}")
        return None


def is_extension_available() -> bool:
    """Check if the cr-sqlite extension is available."""
    ext_path = get_extension_path()
    return ext_path is not None and ext_path.exists()


def load_extension(conn: sqlite3.Connection) -> bool:
    """Load the cr-sqlite extension into a connection.

    This MUST be called as the first operation after opening a connection.

    Args:
        conn: SQLite connection to load extension into.

    Returns:
        True if extension was loaded successfully.
    """
    ext_path = get_extension_path()

    if ext_path is None or not ext_path.exists():
        logger.warning("cr-sqlite extension not found, attempting download...")
        ext_path = download_extension()
        if ext_path is None:
            return False

    try:
        conn.enable_load_extension(True)
        # Load with explicit entry point using SQL function for compatibility
        # The entry point "sqlite3_crsqlite_init" is required by cr-sqlite
        ext_path_str = str(ext_path.with_suffix(""))
        conn.execute("SELECT load_extension(?, ?)", (ext_path_str, "sqlite3_crsqlite_init"))
        logger.debug("cr-sqlite extension loaded successfully")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to load cr-sqlite extension: {e}")
        return False


def finalize_connection(conn: sqlite3.Connection) -> None:
    """Finalize cr-sqlite before closing connection.

    This should be called before closing any connection that loaded cr-sqlite.
    """
    try:
        conn.execute("SELECT crsql_finalize()")
    except sqlite3.Error as e:
        logger.warning(f"Failed to finalize cr-sqlite: {e}")


def get_site_id(conn: sqlite3.Connection) -> bytes | None:
    """Get the unique site ID for this database instance.

    Each database has a unique site_id that identifies it in the sync network.

    Returns:
        Site ID as bytes, or None if cr-sqlite not loaded.
    """
    try:
        cursor = conn.execute("SELECT crsql_site_id()")
        row = cursor.fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def get_db_version(conn: sqlite3.Connection) -> int:
    """Get the current database version (logical clock).

    This version increments with each change and is used to track
    what changes have been synced.

    Returns:
        Current db_version, or -1 if cr-sqlite not loaded.
    """
    try:
        cursor = conn.execute("SELECT crsql_db_version()")
        row = cursor.fetchone()
        return row[0] if row else -1
    except sqlite3.Error:
        return -1


def upgrade_to_crr(conn: sqlite3.Connection, table_name: str) -> bool:
    """Convert a table to a Conflict-free Replicated Relation (CRR).

    This enables the table for multi-master replication with automatic
    conflict resolution using CRDTs.

    Args:
        conn: Database connection with cr-sqlite loaded.
        table_name: Name of the table to upgrade.

    Returns:
        True if upgrade was successful.
    """
    try:
        conn.execute(f"SELECT crsql_as_crr('{table_name}')")
        conn.commit()
        logger.info(f"Upgraded table '{table_name}' to CRR")
        return True
    except sqlite3.Error as e:
        logger.error(f"Failed to upgrade table '{table_name}' to CRR: {e}")
        return False


def get_changes_since(
    conn: sqlite3.Connection,
    since_version: int = 0,
    exclude_site_id: bytes | None = None,
) -> list[dict]:
    """Get all changes since a given database version.

    Args:
        conn: Database connection with cr-sqlite loaded.
        since_version: Only return changes after this version.
        exclude_site_id: Exclude changes from this site (usually our own).

    Returns:
        List of change records that can be sent to other peers.
    """
    changes = []

    try:
        if exclude_site_id:
            cursor = conn.execute(
                """
                SELECT "table", "pk", "cid", "val", "col_version",
                       "db_version", "site_id", "cl", "seq"
                FROM crsql_changes
                WHERE db_version > ?
                AND site_id IS NOT ?
                ORDER BY db_version, seq
                """,
                (since_version, exclude_site_id),
            )
        else:
            cursor = conn.execute(
                """
                SELECT "table", "pk", "cid", "val", "col_version",
                       "db_version", "site_id", "cl", "seq"
                FROM crsql_changes
                WHERE db_version > ?
                ORDER BY db_version, seq
                """,
                (since_version,),
            )

        for row in cursor:
            changes.append(
                {
                    "table": row[0],
                    "pk": row[1],
                    "cid": row[2],
                    "val": row[3],
                    "col_version": row[4],
                    "db_version": row[5],
                    "site_id": row[6].hex() if row[6] else None,
                    "cl": row[7],
                    "seq": row[8],
                }
            )

        return changes

    except sqlite3.Error as e:
        logger.error(f"Failed to get changes: {e}")
        return []


def apply_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    """Apply changes from another peer to this database.

    Args:
        conn: Database connection with cr-sqlite loaded.
        changes: List of change records from get_changes_since().

    Returns:
        Number of changes applied.
    """
    applied = 0

    try:
        for change in changes:
            # Convert hex site_id back to bytes
            site_id = bytes.fromhex(change["site_id"]) if change["site_id"] else None

            conn.execute(
                """
                INSERT INTO crsql_changes
                    ("table", "pk", "cid", "val", "col_version",
                     "db_version", "site_id", "cl", "seq")
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change["table"],
                    change["pk"],
                    change["cid"],
                    change["val"],
                    change["col_version"],
                    change["db_version"],
                    site_id,
                    change["cl"],
                    change["seq"],
                ),
            )
            applied += 1

        conn.commit()
        logger.info(f"Applied {applied} changes from peer")
        return applied

    except sqlite3.Error as e:
        logger.error(f"Failed to apply changes: {e}")
        conn.rollback()
        return 0


def export_changes_to_file(
    conn: sqlite3.Connection,
    output_path: Path,
    since_version: int = 0,
) -> dict:
    """Export changes to a JSON file for offline sync.

    Args:
        conn: Database connection with cr-sqlite loaded.
        output_path: Path to write the JSON file.
        since_version: Only export changes after this version.

    Returns:
        Dict with export metadata (site_id, db_version, change_count).
    """
    site_id = get_site_id(conn)
    db_version = get_db_version(conn)
    changes = get_changes_since(conn, since_version)

    export_data = {
        "format": "lestash-crsqlite-v1",
        "site_id": site_id.hex() if site_id else None,
        "db_version": db_version,
        "since_version": since_version,
        "change_count": len(changes),
        "changes": changes,
    }

    output_path.write_text(json.dumps(export_data, indent=2))

    return {
        "site_id": export_data["site_id"],
        "db_version": db_version,
        "change_count": len(changes),
        "output_path": str(output_path),
    }


def import_changes_from_file(
    conn: sqlite3.Connection,
    input_path: Path,
) -> dict:
    """Import changes from a JSON file.

    Args:
        conn: Database connection with cr-sqlite loaded.
        input_path: Path to the JSON file to import.

    Returns:
        Dict with import metadata (changes_applied, source_site_id).
    """
    data = json.loads(input_path.read_text())

    if data.get("format") != "lestash-crsqlite-v1":
        raise ValueError(f"Unknown export format: {data.get('format')}")

    changes = data.get("changes", [])
    applied = apply_changes(conn, changes)

    return {
        "source_site_id": data.get("site_id"),
        "source_db_version": data.get("db_version"),
        "changes_received": len(changes),
        "changes_applied": applied,
    }


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Rebuild the FTS5 index after applying sync changes.

    cr-sqlite changes bypass triggers, so FTS needs manual rebuild.

    Args:
        conn: Database connection.

    Returns:
        Number of items re-indexed.
    """
    try:
        # Delete all FTS entries
        conn.execute("DELETE FROM items_fts")

        # Rebuild from items table
        conn.execute(
            """
            INSERT INTO items_fts(rowid, title, content, author)
            SELECT id, title, content, author FROM items
            """
        )
        conn.commit()

        # Get count
        count_cursor = conn.execute("SELECT COUNT(*) FROM items_fts")
        count = count_cursor.fetchone()[0]

        logger.info(f"Rebuilt FTS index with {count} items")
        return count

    except sqlite3.Error as e:
        logger.error(f"Failed to rebuild FTS index: {e}")
        return 0

"""Database management for Le Stash.

Schema versioning uses PRAGMA user_version to track migrations.
Each migration is applied once, incrementing the version number.

For CRDT-based sync, see the crsqlite module which provides:
- Extension loading for cr-sqlite
- Change tracking and application
- FTS index rebuilding after sync
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lestash.core.config import Config

logger = logging.getLogger(__name__)

# Current schema version - increment when adding migrations
SCHEMA_VERSION = 3

# Base schema (version 0) - applied to new databases
# CRR-compatible: no UNIQUE constraints (besides PK), NOT NULL columns have DEFAULTs
SCHEMA = """
-- Generic content items (works for all sources)
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT,
    url TEXT,
    title TEXT,
    content TEXT NOT NULL DEFAULT '',
    author TEXT,
    created_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_own_content BOOLEAN DEFAULT FALSE,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_type);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_items_own ON items(is_own_content);

-- Full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content, author,
    content=items, content_rowid=id
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, content, author)
    VALUES (new.id, new.title, new.content, new.author);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, author)
    VALUES ('delete', old.id, old.title, old.content, old.author);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content, author)
    VALUES ('delete', old.id, old.title, old.content, old.author);
    INSERT INTO items_fts(rowid, title, content, author)
    VALUES (new.id, new.title, new.content, new.author);
END;

-- Tags for organization
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY NOT NULL,
    name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL DEFAULT 0,
    tag_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, tag_id)
);

-- Source configurations (per-plugin settings)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    config TEXT,
    last_sync DATETIME,
    enabled BOOLEAN DEFAULT TRUE
);

-- Sync history
CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    started_at DATETIME NOT NULL,
    completed_at DATETIME,
    status TEXT NOT NULL,
    items_added INTEGER DEFAULT 0,
    items_updated INTEGER DEFAULT 0,
    error_message TEXT
);

-- Log entries (optional queryable log history)
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    extra TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_timestamp ON log_entries(timestamp);
CREATE INDEX IF NOT EXISTS idx_log_level ON log_entries(level);
"""

# Migrations: list of (version, description, sql) tuples
# Each migration brings the database from version N-1 to version N
MIGRATIONS = [
    (
        1,
        "Add item_history table for audit trail",
        """
        -- Item history for tracking changes (audit trail)
        CREATE TABLE IF NOT EXISTS item_history (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            content_old TEXT,
            title_old TEXT,
            author_old TEXT,
            url_old TEXT,
            metadata_old TEXT,
            is_own_content_old BOOLEAN,
            changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            change_reason TEXT,
            change_type TEXT DEFAULT 'update'
        );

        CREATE INDEX IF NOT EXISTS idx_item_history_item ON item_history(item_id);
        CREATE INDEX IF NOT EXISTS idx_item_history_changed_at ON item_history(changed_at);

        -- Trigger to capture history before updates to tracked fields
        CREATE TRIGGER IF NOT EXISTS capture_item_history
        BEFORE UPDATE ON items
        FOR EACH ROW
        WHEN OLD.content != NEW.content
          OR OLD.author IS NOT NEW.author
          OR OLD.metadata != NEW.metadata
        BEGIN
            INSERT INTO item_history (
                item_id, content_old, title_old, author_old,
                url_old, metadata_old, is_own_content_old,
                change_reason, change_type
            ) VALUES (
                OLD.id, OLD.content, OLD.title, OLD.author,
                OLD.url, OLD.metadata, OLD.is_own_content,
                'api-update', 'update'
            );
        END;
        """,
    ),
    (
        2,
        "Add person_profiles table for URN to profile URL mapping",
        """
        -- Person profiles lookup table (maps URNs to profile URLs)
        CREATE TABLE IF NOT EXISTS person_profiles (
            id INTEGER PRIMARY KEY,
            urn TEXT UNIQUE NOT NULL,
            profile_url TEXT,
            display_name TEXT,
            source TEXT DEFAULT 'manual',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_person_profiles_urn ON person_profiles(urn);
        """,
    ),
    (
        3,
        "Make tables CRR-compatible (drop UNIQUE constraints, add DEFAULTs)",
        """
        -- Migration 3: Make tables compatible with cr-sqlite CRRs.
        -- CRRs cannot have UNIQUE indices besides the primary key, and
        -- NOT NULL columns must have DEFAULT values.

        -- === items table ===
        -- Drop FTS triggers and virtual table first (they reference items by name)
        DROP TRIGGER IF EXISTS items_ai;
        DROP TRIGGER IF EXISTS items_ad;
        DROP TRIGGER IF EXISTS items_au;
        DROP TRIGGER IF EXISTS capture_item_history;
        DROP TABLE IF EXISTS items_fts;

        -- Recreate items without UNIQUE constraint, with DEFAULTs
        CREATE TABLE items_new (
            id INTEGER PRIMARY KEY NOT NULL,
            source_type TEXT NOT NULL DEFAULT '',
            source_id TEXT,
            url TEXT,
            title TEXT,
            content TEXT NOT NULL DEFAULT '',
            author TEXT,
            created_at DATETIME,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_own_content BOOLEAN DEFAULT FALSE,
            metadata TEXT
        );
        INSERT INTO items_new SELECT * FROM items;
        DROP TABLE items;
        ALTER TABLE items_new RENAME TO items;

        CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_type);
        CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);
        CREATE INDEX IF NOT EXISTS idx_items_own ON items(is_own_content);

        -- Recreate FTS virtual table and triggers
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            title, content, author,
            content=items, content_rowid=id
        );

        CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
            INSERT INTO items_fts(rowid, title, content, author)
            VALUES (new.id, new.title, new.content, new.author);
        END;

        CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
            INSERT INTO items_fts(items_fts, rowid, title, content, author)
            VALUES ('delete', old.id, old.title, old.content, old.author);
        END;

        CREATE TRIGGER items_au AFTER UPDATE ON items BEGIN
            INSERT INTO items_fts(items_fts, rowid, title, content, author)
            VALUES ('delete', old.id, old.title, old.content, old.author);
            INSERT INTO items_fts(rowid, title, content, author)
            VALUES (new.id, new.title, new.content, new.author);
        END;

        -- Recreate history trigger (was dropped with items table)
        CREATE TRIGGER capture_item_history
        BEFORE UPDATE ON items
        FOR EACH ROW
        WHEN OLD.content != NEW.content
          OR OLD.author IS NOT NEW.author
          OR OLD.metadata != NEW.metadata
        BEGIN
            INSERT INTO item_history (
                item_id, content_old, title_old, author_old,
                url_old, metadata_old, is_own_content_old,
                change_reason, change_type
            ) VALUES (
                OLD.id, OLD.content, OLD.title, OLD.author,
                OLD.url, OLD.metadata, OLD.is_own_content,
                'api-update', 'update'
            );
        END;

        -- Rebuild FTS index with existing data
        INSERT INTO items_fts(items_fts) VALUES('rebuild');

        -- === tags table ===
        CREATE TABLE tags_new (
            id INTEGER PRIMARY KEY NOT NULL,
            name TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO tags_new SELECT * FROM tags;
        DROP TABLE tags;
        ALTER TABLE tags_new RENAME TO tags;

        -- === item_tags table ===
        CREATE TABLE item_tags_new (
            item_id INTEGER NOT NULL DEFAULT 0,
            tag_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (item_id, tag_id)
        );
        INSERT INTO item_tags_new SELECT * FROM item_tags;
        DROP TABLE item_tags;
        ALTER TABLE item_tags_new RENAME TO item_tags;

        -- === sources table ===
        CREATE TABLE sources_new (
            id INTEGER PRIMARY KEY NOT NULL,
            source_type TEXT NOT NULL DEFAULT '',
            config TEXT,
            last_sync DATETIME,
            enabled BOOLEAN DEFAULT TRUE
        );
        INSERT INTO sources_new SELECT * FROM sources;
        DROP TABLE sources;
        ALTER TABLE sources_new RENAME TO sources;

        -- === person_profiles table ===
        CREATE TABLE person_profiles_new (
            id INTEGER PRIMARY KEY NOT NULL,
            urn TEXT NOT NULL DEFAULT '',
            profile_url TEXT,
            display_name TEXT,
            source TEXT DEFAULT 'manual',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO person_profiles_new SELECT * FROM person_profiles;
        DROP TABLE person_profiles;
        ALTER TABLE person_profiles_new RENAME TO person_profiles;

        CREATE INDEX IF NOT EXISTS idx_person_profiles_urn ON person_profiles(urn);
        """,
    ),
]


def get_db_path(config: Config | None = None) -> Path:
    """Get the database path from config."""
    if config is None:
        config = Config.load()
    return Path(config.general.database_path).expanduser()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the database."""
    cursor = conn.execute("PRAGMA user_version")
    return cursor.fetchone()[0]


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version in the database."""
    conn.execute(f"PRAGMA user_version = {version}")


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations.

    Returns:
        Number of migrations applied.
    """
    current_version = get_schema_version(conn)
    applied = 0

    for version, description, sql in MIGRATIONS:
        if version > current_version:
            logger.info(f"Applying migration {version}: {description}")
            conn.executescript(sql)
            set_schema_version(conn, version)
            conn.commit()
            applied += 1
            logger.info(f"Migration {version} complete")

    return applied


def init_database(config: Config | None = None) -> None:
    """Initialize the database with schema and apply migrations."""
    db_path = get_db_path(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        # Apply base schema
        conn.executescript(SCHEMA)
        conn.commit()

        # Apply any migrations
        apply_migrations(conn)


@contextmanager
def get_connection(config: Config | None = None) -> Iterator[sqlite3.Connection]:
    """Get a database connection.

    Automatically initializes database if needed and applies pending migrations.
    """
    db_path = get_db_path(config)
    if not db_path.exists():
        init_database(config)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Apply any pending migrations
    current_version = get_schema_version(conn)
    if current_version < SCHEMA_VERSION:
        apply_migrations(conn)

    try:
        yield conn
    finally:
        conn.close()


def upsert_item(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: str,
    url: str | None = None,
    title: str | None = None,
    content: str = "",
    author: str | None = None,
    created_at: str | None = None,
    is_own_content: bool = False,
    metadata: str | None = None,
) -> int:
    """Insert or update an item by (source_type, source_id).

    Does NOT call conn.commit() — caller must commit.

    Returns:
        1 if a new item was inserted, 0 if an existing item was updated.
    """
    cursor = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )
    existing = cursor.fetchone()

    if existing:
        row_id = existing[0] if isinstance(existing, tuple) else existing["id"]
        conn.execute(
            """UPDATE items SET
                url = ?, title = ?, content = ?, author = ?,
                is_own_content = ?, metadata = ?
            WHERE id = ?""",
            (url, title, content, author, is_own_content, metadata, row_id),
        )
        return 0
    else:
        conn.execute(
            """INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type,
                source_id,
                url,
                title,
                content,
                author,
                created_at,
                is_own_content,
                metadata,
            ),
        )
        return 1


def upsert_source(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    last_sync: str | None = None,
) -> None:
    """Insert or update a source's last_sync timestamp.

    Does NOT call conn.commit() — caller must commit.
    """
    cursor = conn.execute(
        "SELECT id FROM sources WHERE source_type = ?",
        (source_type,),
    )
    existing = cursor.fetchone()

    if existing:
        conn.execute(
            "UPDATE sources SET last_sync = ? WHERE source_type = ?",
            (last_sync, source_type),
        )
    else:
        conn.execute(
            "INSERT INTO sources (source_type, last_sync) VALUES (?, ?)",
            (source_type, last_sync),
        )


def get_person_profile(conn: sqlite3.Connection, urn: str) -> dict | None:
    """Look up a person profile by URN.

    Args:
        conn: Database connection
        urn: LinkedIn person URN (e.g., "urn:li:person:xu59iSkkD6")

    Returns:
        Dict with profile_url, display_name, etc. or None if not found
    """
    cursor = conn.execute(
        "SELECT urn, profile_url, display_name, source FROM person_profiles WHERE urn = ?",
        (urn,),
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def upsert_person_profile(
    conn: sqlite3.Connection,
    urn: str,
    profile_url: str | None = None,
    display_name: str | None = None,
    source: str = "manual",
) -> None:
    """Add or update a person profile mapping.

    Args:
        conn: Database connection
        urn: LinkedIn person URN
        profile_url: LinkedIn profile URL (e.g., "https://linkedin.com/in/john-doe")
        display_name: Human-readable name
        source: How the mapping was obtained (manual, api, etc.)
    """
    cursor = conn.execute(
        "SELECT profile_url, display_name FROM person_profiles WHERE urn = ?",
        (urn,),
    )
    existing = cursor.fetchone()

    if existing:
        # Preserve existing values when new value is None (COALESCE behavior)
        new_profile_url = profile_url if profile_url is not None else existing[0]
        new_display_name = display_name if display_name is not None else existing[1]
        conn.execute(
            """UPDATE person_profiles SET
                profile_url = ?, display_name = ?, source = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE urn = ?""",
            (new_profile_url, new_display_name, source, urn),
        )
    else:
        conn.execute(
            """INSERT INTO person_profiles (urn, profile_url, display_name, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (urn, profile_url, display_name, source),
        )
    conn.commit()


def list_person_profiles(conn: sqlite3.Connection) -> list[dict]:
    """List all person profile mappings.

    Returns:
        List of profile dicts with urn, profile_url, display_name, source
    """
    cursor = conn.execute(
        """SELECT urn, profile_url, display_name, source
        FROM person_profiles ORDER BY display_name, urn"""
    )
    return [dict(row) for row in cursor.fetchall()]


def delete_person_profile(conn: sqlite3.Connection, urn: str) -> bool:
    """Delete a person profile mapping.

    Returns:
        True if a profile was deleted, False if not found
    """
    cursor = conn.execute("DELETE FROM person_profiles WHERE urn = ?", (urn,))
    conn.commit()
    return cursor.rowcount > 0


@contextmanager
def get_crdt_connection(config: Config | None = None) -> Iterator[sqlite3.Connection]:
    """Get a database connection with cr-sqlite extension loaded.

    This connection supports CRDT-based sync operations. The cr-sqlite
    extension MUST be loaded as the first operation, so this is a separate
    context manager from get_connection().

    Use this for sync operations. For regular database access, use get_connection().

    Important: Call crsqlite.finalize_connection(conn) before the connection closes.
    This is handled automatically by this context manager.
    """
    from lestash.core import crsqlite

    db_path = get_db_path(config)
    if not db_path.exists():
        init_database(config)

    conn = sqlite3.connect(db_path)

    # Load cr-sqlite as FIRST operation (required by cr-sqlite)
    if not crsqlite.load_extension(conn):
        conn.close()
        raise RuntimeError(
            "Failed to load cr-sqlite extension. " "Run 'lestash sync setup' to download it."
        )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Apply any pending migrations
    current_version = get_schema_version(conn)
    if current_version < SCHEMA_VERSION:
        apply_migrations(conn)

    try:
        yield conn
    finally:
        # Finalize cr-sqlite before closing
        crsqlite.finalize_connection(conn)
        conn.close()


def is_crdt_enabled(conn: sqlite3.Connection) -> bool:
    """Check if the database has CRDT sync enabled (tables upgraded to CRRs).

    Returns:
        True if the items table is a CRR.
    """
    try:
        # Check if crsql_changes table exists (indicates cr-sqlite is active)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crsql_changes'"
        )
        return cursor.fetchone() is not None
    except sqlite3.Error:
        return False

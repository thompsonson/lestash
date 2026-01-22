"""Database management for Le Stash.

Schema versioning uses PRAGMA user_version to track migrations.
Each migration is applied once, incrementing the version number.
"""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lestash.core.config import Config

logger = logging.getLogger(__name__)

# Current schema version - increment when adding migrations
SCHEMA_VERSION = 2

# Base schema (version 0) - applied to new databases
SCHEMA = """
-- Generic content items (works for all sources)
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT,
    url TEXT,
    title TEXT,
    content TEXT NOT NULL,
    author TEXT,
    created_at DATETIME,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_own_content BOOLEAN DEFAULT FALSE,
    metadata TEXT,
    UNIQUE(source_type, source_id)
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
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);

-- Source configurations (per-plugin settings)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_type TEXT UNIQUE NOT NULL,
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
    conn.execute(
        """
        INSERT INTO person_profiles (urn, profile_url, display_name, source, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(urn) DO UPDATE SET
            profile_url = COALESCE(excluded.profile_url, profile_url),
            display_name = COALESCE(excluded.display_name, display_name),
            source = excluded.source,
            updated_at = CURRENT_TIMESTAMP
        """,
        (urn, profile_url, display_name, source),
    )
    conn.commit()


def list_person_profiles(conn: sqlite3.Connection) -> list[dict]:
    """List all person profile mappings.

    Returns:
        List of profile dicts with urn, profile_url, display_name, source
    """
    cursor = conn.execute(
        "SELECT urn, profile_url, display_name, source FROM person_profiles ORDER BY display_name, urn"
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

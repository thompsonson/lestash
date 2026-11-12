"""Database management for Le Stash.

Schema versioning uses PRAGMA user_version to track migrations.
Each migration is applied once, incrementing the version number.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from lestash.core.activities import record_activities
from lestash.core.config import Config

if TYPE_CHECKING:
    from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

# Current schema version - increment when adding migrations
SCHEMA_VERSION = 26

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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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

-- Tags for organization. `kind` distinguishes curated 'category' tags
-- (the composer's autocomplete list) from free-form 'ad-hoc' tags.
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'ad-hoc'
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

-- Syndications: audit row per (item, target, target_url). Wave 2a slice 3.
-- The Publisher protocol returns a PublishResult; the route writes one row
-- here so subsequent publish() calls can short-circuit via AlreadyPublished
-- (unless the caller opts in to re-publish).
CREATE TABLE IF NOT EXISTS syndications (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    target_url TEXT NOT NULL,
    published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    request_body TEXT,
    response_body TEXT,
    UNIQUE(item_id, target, target_url)
);
CREATE INDEX IF NOT EXISTS idx_syndications_item ON syndications(item_id);
"""

# Activities (docs/content-and-activity-ontology-design.md): one row per time someone
# did something to an item. Migrations 14 and 15 filled them from the times lists that
# imports kept in item metadata; migration 16 removed the lists. Imports now attach
# activities to their items, stored by core.activities.record_activities().
ACTIVITY_VERBS = {
    "watched_times": "watched",
    "viewed_times": "viewed",
    "searched_times": "searched",
    "prompted_times": "prompted",
}


def _activities_from_metadata(row: str, dates_only: bool = False) -> str:
    """An INSERT of one activity per time in each times list of ``row``'s metadata.

    ``row`` is ``items`` (the backfill, every row) or ``NEW`` (a trigger, one row). Every
    list comes from a Takeout import, hence the source. Times are stored in UTC, as
    2026-11-08T17:35:03.123Z; one that won't parse is skipped.

    A time already recorded is skipped by NOT EXISTS, not OR IGNORE: inside a trigger the
    outer statement's conflict handling replaces OR IGNORE, so an upsert would fail.
    Triggers can't use WITH, so the verbs are a UNION ALL subquery.

    ``dates_only`` (migration 15 on) skips anything not starting with a date, such as
    "now", "10:00" or a number, which strftime would turn into a time. Migration 14 ran
    without it on the live database, so its SQL stays as it was.
    """
    verbs = " UNION ALL ".join(
        f"SELECT '{verb}' AS verb, '$.{key}' AS path" for key, verb in ACTIVITY_VERBS.items()
    )
    # json_each fails on invalid JSON, and SQLite may run it before a WHERE test
    metadata = f"CASE WHEN json_valid({row}.metadata) THEN {row}.metadata ELSE '{{}}' END"
    tables = "items, " if row == "items" else ""
    source = f"'takeout:' || {row}.source_type"
    at = "strftime('%Y-%m-%dT%H:%M:%fZ', t.value)"
    dates = " AND t.value GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'" if dates_only else ""
    return f"""
    INSERT INTO activities (verb, item_id, occurred_at, source, details)
    SELECT DISTINCT v.verb, {row}.id, {at}, {source},
           CASE WHEN v.verb = 'searched'
                THEN json_object('query', json_extract({metadata}, '$.query')) END
    FROM {tables}({verbs}) AS v, json_each({metadata}, v.path) AS t
    WHERE json_type({metadata}, v.path) = 'array' AND t.type = 'text' AND {at} IS NOT NULL{dates}
      AND NOT EXISTS (
          SELECT 1 FROM activities a
          WHERE a.verb = v.verb AND a.item_id = {row}.id AND a.occurred_at = {at}
            AND a.source = {source}
      );
    """


_HAS_TIMES = " OR ".join(f"json_type(metadata, '$.{key}') IS NOT NULL" for key in ACTIVITY_VERBS)

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
        "Add post_cache table for enriching reactions and comments",
        """
        -- Post cache for storing content of posts that reactions/comments reference
        CREATE TABLE IF NOT EXISTS post_cache (
            id INTEGER PRIMARY KEY,
            urn TEXT UNIQUE NOT NULL,
            author_urn TEXT,
            author_name TEXT,
            content_preview TEXT,
            full_content TEXT,
            image_path TEXT,
            url TEXT,
            created_at DATETIME,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT DEFAULT 'manual'
        );

        CREATE INDEX IF NOT EXISTS idx_post_cache_urn ON post_cache(urn);
        """,
    ),
    (
        4,
        "Add reactor_name column to post_cache for tracking who reacted to your content",
        """
        -- Add reactor_name to store who reacted to your content
        ALTER TABLE post_cache ADD COLUMN reactor_name TEXT;
        """,
    ),
    (
        5,
        "Add parent_id column to items for parent-child relationships",
        # Note: no IF NOT EXISTS for ALTER TABLE ADD COLUMN in SQLite,
        # so we check via PRAGMA before altering. Using executescript
        # doesn't support conditionals, so this is handled specially
        # in apply_migrations().
        """
        CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id);
        """,
    ),
    (
        6,
        "Add collections tables for cross-source item grouping",
        """
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
            item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            note TEXT,
            PRIMARY KEY (collection_id, item_id)
        );

        CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id);
        """,
    ),
    (
        7,
        "Add item_media table for generic media/attachment support",
        """
        CREATE TABLE IF NOT EXISTS item_media (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            media_type TEXT NOT NULL,
            url TEXT,
            local_path TEXT,
            mime_type TEXT,
            alt_text TEXT,
            position INTEGER DEFAULT 0,
            source_origin TEXT DEFAULT 'sync',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_item_media_item ON item_media(item_id);
        """,
    ),
    (
        8,
        "Fix history trigger: add title check and use IS NOT for NULL safety",
        """
        DROP TRIGGER IF EXISTS capture_item_history;

        CREATE TRIGGER capture_item_history
        BEFORE UPDATE ON items
        FOR EACH ROW
        WHEN OLD.content IS NOT NEW.content
          OR OLD.title IS NOT NEW.title
          OR OLD.author IS NOT NEW.author
          OR OLD.metadata IS NOT NEW.metadata
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
        9,
        "Capture parent_id in item_history; cleanup duplicate sync history rows",
        # Note: ALTER TABLE ADD COLUMN parent_id_old is handled in apply_migrations()
        # because SQLite has no IF NOT EXISTS for ALTER TABLE.
        """
        DROP TRIGGER IF EXISTS capture_item_history;

        CREATE TRIGGER capture_item_history
        BEFORE UPDATE ON items
        FOR EACH ROW
        WHEN OLD.content IS NOT NEW.content
          OR OLD.title IS NOT NEW.title
          OR OLD.author IS NOT NEW.author
          OR OLD.metadata IS NOT NEW.metadata
          OR OLD.parent_id IS NOT NEW.parent_id
        BEGIN
            INSERT INTO item_history (
                item_id, content_old, title_old, author_old,
                url_old, metadata_old, is_own_content_old,
                parent_id_old, change_reason, change_type
            ) VALUES (
                OLD.id, OLD.content, OLD.title, OLD.author,
                OLD.url, OLD.metadata, OLD.is_own_content,
                OLD.parent_id, 'api-update', 'update'
            );
        END;

        DELETE FROM item_history
        WHERE id NOT IN (
            SELECT MIN(ih.id) FROM item_history ih
            JOIN items i ON i.id = ih.item_id
            WHERE i.source_type IN ('linkedin', 'youtube', 'audible', 'bluesky')
            GROUP BY ih.item_id,
                     COALESCE(ih.content_old, ''),
                     COALESCE(ih.title_old, ''),
                     COALESCE(ih.metadata_old, '')
        )
        AND item_id IN (
            SELECT id FROM items
            WHERE source_type IN ('linkedin', 'youtube', 'audible', 'bluesky')
        );
        """,
    ),
    (
        10,
        "Add tags.kind column (category vs ad-hoc)",
        # Note: ALTER TABLE ADD COLUMN tags.kind is handled in apply_migrations()
        # because SQLite has no IF NOT EXISTS for ALTER TABLE.
        # The column is purely additive — existing rows get the DEFAULT 'ad-hoc'.
        # No data backfill in the migration: which tags are categories is a
        # user-curation decision, not a schema concern.
        "",
    ),
    (
        11,
        "Add syndications table (Wave 2a slice 3 — Publisher audit)",
        """
        CREATE TABLE IF NOT EXISTS syndications (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            target TEXT NOT NULL,
            target_url TEXT NOT NULL,
            published_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            request_body TEXT,
            response_body TEXT,
            UNIQUE(item_id, target, target_url)
        );
        CREATE INDEX IF NOT EXISTS idx_syndications_item ON syndications(item_id);
        """,
    ),
    (
        12,
        "Backfill items.created_at NULLs with fetched_at",
        # items.created_at had no default, so rows written without a date
        # (API-created notes, gemini imports, etc.) stored NULL. fetched_at is
        # the best available capture-time proxy. Insert-layer defaults in
        # upsert_item()/create_item() prevent future NULLs.
        "UPDATE items SET created_at = fetched_at WHERE created_at IS NULL;",
    ),
    (
        13,
        "Add sync_checkpoints table (plugin state committed with its items)",
        """
        CREATE TABLE IF NOT EXISTS sync_checkpoints (
            source TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, key)
        );
        """,
    ),
    (
        14,
        "Add activities table, filled from the times lists in item metadata",
        # actor_id (null means you) arrives with the people table: SQLite refuses rows
        # whose foreign key names a table that doesn't exist yet
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY,
            verb TEXT NOT NULL,
            item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
            occurred_at TEXT NOT NULL,
            source TEXT NOT NULL,
            context_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
            details TEXT,
            UNIQUE (verb, item_id, occurred_at, source)
        );
        CREATE INDEX IF NOT EXISTS idx_activities_time ON activities(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_activities_item ON activities(item_id);
        """
        + _activities_from_metadata("items")
        + f"""
        CREATE TRIGGER IF NOT EXISTS items_activities_ai AFTER INSERT ON items
        WHEN instr(NEW.metadata, '_times"') > 0 BEGIN
            {_activities_from_metadata("NEW")}
        END;
        CREATE TRIGGER IF NOT EXISTS items_activities_au AFTER UPDATE OF metadata ON items
        WHEN instr(NEW.metadata, '_times"') > 0 BEGIN
            {_activities_from_metadata("NEW")}
        END;
        """,
    ),
    (
        15,
        "Index activities.context_id; triggers record only date-shaped times",
        # Changes to 14, kept separate because the live database already ran 14 as it
        # stands. Without the index, deleting an item scans every activity for the
        # ON DELETE SET NULL. The triggers are replaced to skip times that aren't dates.
        f"""
        CREATE INDEX IF NOT EXISTS idx_activities_context ON activities(context_id);
        DROP TRIGGER IF EXISTS items_activities_ai;
        DROP TRIGGER IF EXISTS items_activities_au;
        CREATE TRIGGER items_activities_ai AFTER INSERT ON items
        WHEN instr(NEW.metadata, '_times"') > 0 BEGIN
            {_activities_from_metadata("NEW", dates_only=True)}
        END;
        CREATE TRIGGER items_activities_au AFTER UPDATE OF metadata ON items
        WHEN instr(NEW.metadata, '_times"') > 0 BEGIN
            {_activities_from_metadata("NEW", dates_only=True)}
        END;
        """,
    ),
    (
        16,
        "Move the times lists out of item metadata: they are activities now",
        # Run by _migrate_16(): the lists are removed in Python, so the metadata keeps
        # json.dumps' format. SQLite's json_remove() would minify it, and the next
        # import would then rewrite every item, though nothing changed.
        "",
    ),
    (
        17,
        "Add item_links: typed links between items, such as a post that links another",
        # docs/content-and-activity-ontology-design.md, "Typed links". A rejected link
        # keeps its row, so the linker never adds it again.
        """
        CREATE TABLE IF NOT EXISTS item_links (
            from_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            to_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            evidence TEXT,
            origin TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (from_id, to_id, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_item_links_to ON item_links(to_id, kind);
        """,
    ),
    (
        18,
        "Add items.held: 0 marks a stub, a thing known by its address but not held",
        # docs/content-and-activity-ontology-design.md, "Stub items for things not held".
        # The column is added in apply_migrations: SQLite has no ADD COLUMN IF NOT EXISTS.
        "",
    ),
    (
        19,
        "Add people, their accounts, item agents and mentions",
        # docs/content-and-activity-ontology-design.md, "The people ontology". Also adds
        # activities.actor_id (in apply_migrations), now that people exists: null is you.
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            is_self INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_people_self ON people(is_self) WHERE is_self = 1;

        CREATE TABLE IF NOT EXISTS person_identities (
            id INTEGER PRIMARY KEY,
            person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
            platform TEXT NOT NULL,
            identifier TEXT NOT NULL,
            display_name TEXT,
            url TEXT,
            status TEXT NOT NULL DEFAULT 'suggested',
            evidence TEXT,
            origin TEXT NOT NULL,
            UNIQUE (platform, identifier)
        );
        CREATE INDEX IF NOT EXISTS idx_person_identities_person ON person_identities(person_id);

        CREATE TABLE IF NOT EXISTS item_agents (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            position INTEGER,
            evidence TEXT,
            origin TEXT NOT NULL,
            PRIMARY KEY (item_id, person_id, role)
        );
        CREATE INDEX IF NOT EXISTS idx_item_agents_person ON item_agents(person_id, role);

        CREATE TABLE IF NOT EXISTS item_mentions (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
            tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'suggested',
            evidence TEXT,
            origin TEXT NOT NULL,
            CHECK ((person_id IS NULL) <> (tag_id IS NULL))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_item_mentions_person
            ON item_mentions(item_id, person_id) WHERE person_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_item_mentions_tag
            ON item_mentions(item_id, tag_id) WHERE tag_id IS NOT NULL;
        """,
    ),
    (
        20,
        "Move person_profiles into people and their LinkedIn accounts, and drop it",
        # Run by _migrate_20(). Adds people.name_origin: who set a person's name, null
        # when the agents pass did, so the pass never renames a person you named.
        "",
    ),
    (
        21,
        "Key activities by their actor too: two people can like an item at one time",
        # Run by _migrate_21(): SQLite can't change a table's UNIQUE, so the table is
        # rebuilt. Null is you, so the key reads it as 0 (nulls are never equal).
        "",
    ),
    (
        22,
        "Add blobs, the file store's index, and versioned media pointing at it",
        # #217 (W2). item_media's columns are added in apply_migrations. A media row
        # whose superseded_at is set is an older version, kept.
        """
        CREATE TABLE IF NOT EXISTS blobs (
            sha256 TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mime_type TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ),
    (
        23,
        "Add blob_text: the text read from a stored file, by the tool that read it",
        # #218 (W3): read once per file; an item's text is built from it
        """
        CREATE TABLE IF NOT EXISTS blob_text (
            sha256 TEXT NOT NULL REFERENCES blobs(sha256),
            tool TEXT NOT NULL,
            tool_version TEXT,
            text TEXT NOT NULL,
            extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (sha256, tool)
        );
        """,
    ),
    (
        24,
        "Add items.kind: what an item is (post, book, video...), generated from its source",
        # #261 (U2). Run in apply_migrations: SQLite's ADD COLUMN has no IF NOT EXISTS. A
        # virtual generated column, so every writer gets it for free. Its rule is
        # core/kinds.py's KIND_SQL; changing that rule needs a migration that drops and
        # re-adds the column.
        "",
    ),
    (
        25,
        'Add people.short_name: what the app calls a person in a byline ("by Matt")',
        # Run in apply_migrations: SQLite's ADD COLUMN has no IF NOT EXISTS. Null means
        # the first word of the name.
        "",
    ),
    (
        26,
        "LinkedIn: times in UTC, and others' actions not marked yours (#236)",
        # Run in apply_migrations (_migrate_26): converting local time needs Python.
        "",
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


MIGRATION_16_REASON = "migration 16: times lists moved to activities"


def _migrate_16(conn: sqlite3.Connection) -> None:
    """Drop the activity triggers and move the times lists out of item metadata.

    Imports store their times as activities themselves now. Any listed time not yet an
    activity is recorded first, then the lists are removed; the metadata keeps each
    count, first_at and last_at. item_history keeps the old metadata of every item
    changed, tagged MIGRATION_16_REASON. One transaction, with the schema version.
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # take the write lock now, not mid-migration
    try:
        conn.execute("DROP TRIGGER IF EXISTS items_activities_ai")
        conn.execute("DROP TRIGGER IF EXISTS items_activities_au")
        conn.execute(_activities_from_metadata("items", dates_only=True))
        history_before = max_history_id(conn)
        rows = conn.execute(
            f"SELECT id, metadata FROM items WHERE json_valid(metadata) AND ({_HAS_TIMES})"
        ).fetchall()
        updates = []
        for item_id, text in rows:
            metadata = json.loads(text)
            for key in ACTIVITY_VERBS:
                metadata.pop(key, None)
            updates.append((json.dumps(metadata) if metadata else None, item_id))
        conn.executemany("UPDATE items SET metadata = ? WHERE id = ?", updates)
        conn.execute(
            "UPDATE item_history SET change_reason = ? WHERE id > ?",
            (MIGRATION_16_REASON, history_before),
        )
        set_schema_version(conn, 16)
    except BaseException:
        conn.rollback()
        raise


# The zone LinkedIn's changelog times were written in before #236: pop-mini's
LINKEDIN_LOCAL_ZONE = "Europe/Paris"


def _migrate_26(conn: sqlite3.Connection) -> None:
    """LinkedIn rows written before #236 (#236):

    - A created_at with no zone on a changelog row, or on a post made from LeStash, was
      this machine's local time: it becomes UTC. The snapshot fetch and the export
      import wrote UTC.
    - A changelog row whose actor is not its owner is someone else's comment, reaction,
      message or invitation: it is not your own content.

    One transaction, with the version; history rows are marked "migration 26"."""
    from zoneinfo import ZoneInfo

    local = ZoneInfo(LINKEDIN_LOCAL_ZONE)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        since = max_history_id(conn)
        # The changelog's, and posts made from LeStash (they carry post_urn), were
        # written in local time; the snapshot fetch and the export import wrote UTC
        rows = conn.execute(
            "SELECT id, created_at FROM items WHERE source_type = 'linkedin' "
            "AND created_at IS NOT NULL AND json_valid(metadata) "
            "AND (json_extract(metadata, '$.source') = 'changelog' "
            "OR json_extract(metadata, '$.post_urn') IS NOT NULL)"
        ).fetchall()
        for item_id, created_at in rows:
            try:
                when = datetime.fromisoformat(str(created_at))
            except ValueError:
                continue
            if when.tzinfo is not None:
                continue
            utc = when.replace(tzinfo=local).astimezone(UTC)
            conn.execute(
                "UPDATE items SET created_at = ? WHERE id = ?",
                (utc.isoformat(sep=" "), item_id),
            )
        conn.execute(
            "UPDATE items SET is_own_content = 0 WHERE source_type = 'linkedin' "
            "AND is_own_content = 1 AND json_valid(metadata) "
            "AND json_extract(metadata, '$.raw.actor') IS NOT NULL "
            "AND json_extract(metadata, '$.raw.owner') IS NOT NULL "
            "AND json_extract(metadata, '$.raw.actor') != json_extract(metadata, '$.raw.owner')"
        )
        # The transaction holds the database: every history row since is this one's
        mark_recent_history(conn, since, "migration 26")
        set_schema_version(conn, 26)
    except BaseException:
        conn.rollback()
        raise


def _migrate_20(conn: sqlite3.Connection) -> None:
    """Move each person_profiles row onto its LinkedIn account and person, then drop the
    table. A name you set (source manual or web) becomes the person's name, marked as
    yours; a name a script cut from a mention only names a person still named by their
    URN. A profile with no account yet gets one. One transaction, with the version."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _column_exists(conn, "people", "name_origin"):
            conn.execute("ALTER TABLE people ADD COLUMN name_origin TEXT")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'person_profiles'"
        ).fetchone()
        if exists:
            rows = conn.execute(
                "SELECT urn, display_name, profile_url, coalesce(source, 'manual') "
                "FROM person_profiles ORDER BY id"
            ).fetchall()
            for urn, name, url, source in rows:
                _set_linkedin_name(conn, urn, name, url, None if source == "mention" else source)
            conn.execute("DROP TABLE person_profiles")
        set_schema_version(conn, 20)
    except BaseException:
        conn.rollback()
        raise


def _migrate_21(conn: sqlite3.Connection) -> None:
    """Rebuild activities with a key that includes its actor, keeping every row, id and
    index. One transaction, with the version."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'activities' AND sql IS NOT NULL"
            )
        ]
        conn.execute(
            """
            CREATE TABLE activities_new (
                id INTEGER PRIMARY KEY,
                verb TEXT NOT NULL,
                item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
                occurred_at TEXT NOT NULL,
                source TEXT NOT NULL,
                context_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                details TEXT,
                actor_id INTEGER REFERENCES people(id)
            )
            """
        )
        conn.execute(
            "INSERT INTO activities_new (id, verb, item_id, occurred_at, source, context_id, "
            "details, actor_id) SELECT id, verb, item_id, occurred_at, source, context_id, "
            "details, actor_id FROM activities"
        )
        conn.execute("DROP TABLE activities")
        conn.execute("ALTER TABLE activities_new RENAME TO activities")
        for sql in indexes:
            conn.execute(sql)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_key "
            "ON activities(verb, item_id, occurred_at, source, ifnull(actor_id, 0))"
        )
        set_schema_version(conn, 21)
    except BaseException:
        conn.rollback()
        raise


def linkedin_person(conn: sqlite3.Connection, urn: str) -> int | None:
    """The person a LinkedIn member's or company's URN belongs to, its account made as
    an import would if not yet seen; None if the account is not confirmed."""
    _set_linkedin_name(conn, urn, None, None, None)
    row = conn.execute(
        "SELECT person_id FROM person_identities WHERE platform = 'linkedin' "
        "AND identifier = ? AND status = 'confirmed'",
        (urn,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    # table_xinfo: table_info leaves out generated columns, such as items.kind
    cursor = conn.execute(f"PRAGMA table_xinfo({table})")
    return any(row[1] == column for row in cursor.fetchall())


def max_history_id(conn: sqlite3.Connection) -> int:
    """Snapshot the latest item_history.id for a 'mark recent rows' bookkeeping pattern.

    Pair with mark_recent_history() to tag rows that the capture_item_history
    trigger writes during a subsequent UPDATE/UPSERT.
    """
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM item_history").fetchone()[0]


def mark_recent_history(
    conn: sqlite3.Connection,
    since_id: int,
    change_reason: str,
    *,
    item_ids: Iterable[int] | None = None,
) -> None:
    """Re-tag history rows newer than `since_id` with the given change_reason.

    SQLite triggers cannot receive parameters, so the trigger always writes
    'api-update'. Callers (sync, enricher, user-edit) capture max_history_id()
    before their write and call this after to overwrite the default.

    Pass `item_ids` when the caller knows exactly which items it touched —
    this prevents re-tagging rows produced by an unrelated concurrent writer
    that interleaved between the snapshot and the mark. Sync/enricher paths
    that touch many items by design should leave it None.
    """
    if item_ids is None:
        conn.execute(
            "UPDATE item_history SET change_reason = ? WHERE id > ?",
            (change_reason, since_id),
        )
        return
    ids = list(item_ids)
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE item_history SET change_reason = ? WHERE id > ? AND item_id IN ({placeholders})",
        (change_reason, since_id, *ids),
    )


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
            # Migration 5: add parent_id column (needs special handling
            # because ALTER TABLE ADD COLUMN has no IF NOT EXISTS in SQLite)
            if version == 5 and not _column_exists(conn, "items", "parent_id"):
                conn.execute("ALTER TABLE items ADD COLUMN parent_id INTEGER")
            if version == 9 and not _column_exists(conn, "item_history", "parent_id_old"):
                conn.execute("ALTER TABLE item_history ADD COLUMN parent_id_old INTEGER")
            if version == 10 and not _column_exists(conn, "tags", "kind"):
                conn.execute("ALTER TABLE tags ADD COLUMN kind TEXT NOT NULL DEFAULT 'ad-hoc'")
            if version == 18 and not _column_exists(conn, "items", "held"):
                conn.execute("ALTER TABLE items ADD COLUMN held INTEGER NOT NULL DEFAULT 1")
            if version == 24 and not _column_exists(conn, "items", "kind"):
                from lestash.core.kinds import KIND_SQL

                conn.execute(
                    "ALTER TABLE items ADD COLUMN kind TEXT"
                    f" GENERATED ALWAYS AS ({KIND_SQL}) VIRTUAL"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind, parent_id)")
            if version == 25 and not _column_exists(conn, "people", "short_name"):
                conn.execute("ALTER TABLE people ADD COLUMN short_name TEXT")
            if version == 16:
                _migrate_16(conn)  # sets the version in its own transaction
            elif version == 20:
                _migrate_20(conn)  # likewise
            elif version == 21:
                _migrate_21(conn)  # likewise
            elif version == 26:
                _migrate_26(conn)  # likewise
            else:
                conn.executescript(sql)
                if version == 22:
                    for column, ddl in (
                        ("sha256", "sha256 TEXT REFERENCES blobs(sha256)"),
                        ("version", "version INTEGER NOT NULL DEFAULT 1"),
                        ("superseded_at", "superseded_at DATETIME"),
                    ):
                        if not _column_exists(conn, "item_media", column):
                            conn.execute(f"ALTER TABLE item_media ADD COLUMN {ddl}")
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_item_media_sha256 ON item_media(sha256)"
                    )
                if version == 19 and not _column_exists(conn, "activities", "actor_id"):
                    # After the script: the column refers to people, made there. No delete
                    # action: null means you, so SET NULL would make a deleted person's
                    # activities yours
                    conn.execute(
                        "ALTER TABLE activities ADD COLUMN actor_id INTEGER REFERENCES people(id)"
                    )
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


# How long a write waits for another connection's write lock. SQLite's default (5 s) is
# shorter than a long sync's commit interval, so the server or a second sync would fail
# with "database is locked".
BUSY_TIMEOUT_S = 30.0


@contextmanager
def get_connection(config: Config | None = None) -> Iterator[sqlite3.Connection]:
    """Get a database connection.

    Automatically initializes database if needed and applies pending migrations.
    """
    db_path = get_db_path(config)
    if not db_path.exists():
        init_database(config)

    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    # Apply any pending migrations
    current_version = get_schema_version(conn)
    if current_version < SCHEMA_VERSION:
        apply_migrations(conn)

    try:
        yield conn
    finally:
        conn.close()


def _is_your_linkedin(conn: sqlite3.Connection, urn: str) -> bool:
    """Whether the URN is yours: the owner of a LinkedIn changelog item."""
    return (
        conn.execute(
            "SELECT 1 FROM items WHERE source_type = 'linkedin' AND json_valid(metadata) "
            "AND json_extract(metadata, '$.raw.owner') = ? LIMIT 1",
            (urn,),
        ).fetchone()
        is not None
    )


def _linkedin_person(
    conn: sqlite3.Connection, urn: str, name: str | None, origin: str | None
) -> int:
    """The person a new LinkedIn account of this URN belongs to: you if the URN is
    yours, else a new person, named by the URN until a name is known."""
    named_by = origin if name else None
    if _is_your_linkedin(conn, urn):
        row = conn.execute("SELECT id FROM people WHERE is_self = 1").fetchone()
        if row is not None:
            if named_by:
                conn.execute(
                    "UPDATE people SET name = ?, name_origin = ? WHERE id = ?",
                    (name, named_by, row[0]),
                )
            return int(row[0])
        return int(
            conn.execute(
                "INSERT INTO people (name, kind, is_self, name_origin) "
                "VALUES (?, 'person', 1, ?) RETURNING id",
                (name or "You", named_by),
            ).fetchone()[0]
        )
    kind = "organization" if urn.startswith("urn:li:company:") else "person"
    return int(
        conn.execute(
            "INSERT INTO people (name, kind, name_origin) VALUES (?, ?, ?) RETURNING id",
            (name or urn, kind, named_by),
        ).fetchone()[0]
    )


def _set_linkedin_name(
    conn: sqlite3.Connection,
    urn: str,
    name: str | None,
    url: str | None,
    origin: str | None,
) -> None:
    """Name the person a LinkedIn member's account belongs to, and set its profile URL.

    origin says who set the name, such as manual (the CLI) or web (the app): the
    person takes it, and the agents pass keeps it. None is a name a script found, such
    as one cut from a mention: it only names a person still named by their URN, and
    its URL only fills a blank. An account not yet seen is made, as an import would.
    """
    name = " ".join(name.split()) if name else None
    url = url or None
    row = conn.execute(
        "SELECT i.id, i.person_id, i.display_name, p.name FROM person_identities i "
        # An account not confirmed belongs to no one, as in the agents pass
        "LEFT JOIN people p ON p.id = i.person_id AND i.status = 'confirmed' "
        "WHERE i.platform = 'linkedin' AND i.identifier = ?",
        (urn,),
    ).fetchone()
    if row is not None and row[3] is None:
        return  # not confirmed, or its person deleted: it belongs to no one
    if row is None:
        conn.execute(
            "INSERT INTO person_identities (person_id, platform, identifier, display_name, "
            "url, status, evidence, origin) VALUES (?, 'linkedin', ?, ?, ?, 'confirmed', "
            "'profile', ?)",
            # A name you set is the person's; the account keeps the URN, as an import
            # names it. An import's origin, so claim() moves it to you once it shows yours
            (
                _linkedin_person(conn, urn, name, origin),
                urn,
                urn if origin else (name or urn),
                url,
                "import:linkedin",
            ),
        )
        return
    identity_id, person_id, display, current = row
    if url:
        conn.execute(
            "UPDATE person_identities SET url = ? WHERE id = ?"
            if origin
            else "UPDATE person_identities SET url = coalesce(url, ?) WHERE id = ?",
            (url, identity_id),
        )
    if not name:
        return
    if not origin and display in (None, urn):
        # A script's name is the account's, as LinkedIn writes it; a name you set is
        # only the person's, so forgetting it returns to the account's
        conn.execute(
            "UPDATE person_identities SET display_name = ? WHERE id = ?", (name, identity_id)
        )
    if origin:
        conn.execute(
            "UPDATE people SET name = ?, name_origin = ? WHERE id = ?", (name, origin, person_id)
        )
    elif current == urn:
        conn.execute(
            "UPDATE people SET name = ? WHERE id = ? AND name_origin IS NULL", (name, person_id)
        )


_PROFILE = """
    SELECT i.identifier AS urn, i.url AS profile_url,
        CASE WHEN p.name != i.identifier THEN p.name END AS display_name,
        coalesce(p.name_origin, 'mention') AS source
    FROM person_identities i
    LEFT JOIN people p ON p.id = i.person_id AND i.status = 'confirmed'
    WHERE i.platform = 'linkedin' AND (p.name != i.identifier OR i.url IS NOT NULL)
"""


def get_person_profile(conn: sqlite3.Connection, urn: str) -> dict | None:
    """A LinkedIn member's name and profile URL, from their account and person.

    Returns a dict of urn, profile_url, display_name and source (who named them), or
    None if neither name nor URL is known.
    """
    cursor = conn.execute(_PROFILE + " AND i.identifier = ?", (urn,))
    cursor.row_factory = sqlite3.Row
    row = cursor.fetchone()
    return dict(row) if row else None


def upsert_person_profile(
    conn: sqlite3.Connection,
    urn: str,
    profile_url: str | None = None,
    display_name: str | None = None,
    source: str = "manual",
) -> None:
    """Name a LinkedIn member and set their profile URL; see _set_linkedin_name.

    Args:
        conn: Database connection
        urn: LinkedIn member URN
        profile_url: LinkedIn profile URL (e.g., "https://linkedin.com/in/john-doe")
        display_name: Human-readable name
        source: Who set it (manual, web); "mention" for a name a script found, which
            never replaces a name already known
    """
    _set_linkedin_name(
        conn, urn, display_name, profile_url, None if source == "mention" else source
    )
    conn.commit()


def list_person_profiles(conn: sqlite3.Connection) -> list[dict]:
    """The LinkedIn members with a known name or profile URL.

    Returns:
        List of profile dicts with urn, profile_url, display_name, source
    """
    cursor = conn.execute(_PROFILE + " ORDER BY display_name, urn")
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.fetchall()]


def delete_person_profile(conn: sqlite3.Connection, urn: str) -> bool:
    """Stop keeping the name you set for a LinkedIn member: they take their account's
    name again, and the agents pass may rename them. Your own name stays.

    Returns:
        True if you had set their name, False if not
    """
    cursor = conn.execute(
        "UPDATE people SET name_origin = NULL, name = CASE WHEN is_self = 1 THEN name ELSE "
        "(SELECT coalesce(display_name, identifier) FROM person_identities "
        "WHERE platform = 'linkedin' AND identifier = :urn) END "
        "WHERE name_origin IS NOT NULL AND id = (SELECT person_id FROM person_identities "
        "WHERE platform = 'linkedin' AND identifier = :urn AND status = 'confirmed')",
        {"urn": urn},
    )
    conn.commit()
    return cursor.rowcount > 0


def upsert_item(conn: sqlite3.Connection, item: ItemCreate) -> int:
    """Insert or update an item in the database.

    Args:
        conn: Database connection.
        item: ItemCreate instance to persist.

    Returns:
        The item ID of the inserted or updated row.
    """
    import json

    metadata_json = json.dumps(item.metadata) if item.metadata else None
    created_at = item.created_at or datetime.now(UTC)
    pre_max = max_history_id(conn)
    conn.execute(
        """
        INSERT INTO items (
            source_type, source_id, url, title, content,
            author, created_at, is_own_content, metadata, parent_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            title = excluded.title,
            content = excluded.content,
            author = excluded.author,
            metadata = excluded.metadata,
            -- Keep a parent set earlier (by a resolver, the sweep or by hand) when the
            -- caller doesn't name one
            parent_id = COALESCE(excluded.parent_id, items.parent_id)
        WHERE excluded.title IS NOT items.title
           OR excluded.content IS NOT items.content
           OR excluded.author IS NOT items.author
           OR excluded.metadata IS NOT items.metadata
           OR COALESCE(excluded.parent_id, items.parent_id) IS NOT items.parent_id
        """,
        (
            item.source_type,
            item.source_id,
            item.url,
            item.title,
            item.content,
            item.author,
            created_at,
            item.is_own_content,
            metadata_json,
            item.parent_id,
        ),
    )

    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (item.source_type, item.source_id),
    ).fetchone()
    item_id = row[0]
    record_activities(conn, item_id, item.activities)

    mark_recent_history(conn, pre_max, "sync", item_ids=[item_id])
    conn.commit()

    # Insert any media attachments
    if item.media:
        for media in item.media:
            add_item_media(
                conn,
                item_id,
                media_type=media.media_type,
                url=media.url,
                local_path=media.local_path,
                mime_type=media.mime_type,
                alt_text=media.alt_text,
                position=media.position,
                source_origin=media.source_origin,
                _commit=False,
            )
        conn.commit()

    return item_id


def resolve_parent_id(conn: sqlite3.Connection, item: ItemCreate) -> None:
    """Set ``item.parent_id`` from ``metadata["_parent_source_id"]`` when the parent exists.

    Plugins yield a parent before its children and point each child at the parent's
    ``source_id`` (same ``source_type``). Leaves an explicit ``parent_id`` alone.
    """
    if item.parent_id is not None or not item.metadata:
        return
    parent_source_id = item.metadata.get("_parent_source_id")
    if not parent_source_id or parent_source_id == item.source_id:
        return  # an item naming itself is not its own parent
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (item.source_type, parent_source_id),
    ).fetchone()
    if row:
        item.parent_id = row[0]


def resolve_parent_source_ids(
    conn: sqlite3.Connection, source_types: str | Iterable[str] | None = None
) -> int:
    """Link orphaned children to parents named by ``metadata._parent_source_id``.

    Catches children whose parent arrived later (or in an earlier sync). Only fills
    ``parent_id`` where it is NULL, so plugin-specific resolution is not overridden.
    The parent must have the child's ``source_type``. Pass the source types a sync
    wrote (a plugin can write several, e.g. Takeout); ``None`` sweeps every source.
    History rows written by the link are tagged ``sync``.

    Returns:
        Number of items linked.
    """
    if isinstance(source_types, str):
        source_types = [source_types]
    types = sorted(set(source_types)) if source_types is not None else None
    if types == []:
        return 0

    # json_extract raises on malformed JSON, which would fail the whole sweep: skip such rows
    marker = (
        "CASE WHEN json_valid(items.metadata)"
        " THEN json_extract(items.metadata, '$._parent_source_id') END"
    )
    parent_lookup = f"""
        SELECT p.id FROM items p
        WHERE p.source_type = items.source_type
          AND p.source_id = {marker}
          AND p.id != items.id
    """
    where_source = ""
    params: tuple[str, ...] = ()
    if types:
        where_source = f"AND source_type IN ({','.join('?' * len(types))})"
        params = tuple(types)

    since = max_history_id(conn)
    linked = [
        row[0]
        for row in conn.execute(
            f"""
            UPDATE items SET parent_id = ({parent_lookup})
            WHERE parent_id IS NULL
              AND {marker} IS NOT NULL
              AND EXISTS ({parent_lookup})
              {where_source}
            RETURNING id
            """,
            params,
        ).fetchall()
    ]
    mark_recent_history(conn, since, "sync", item_ids=linked)
    conn.commit()
    return len(linked)


def save_checkpoint(conn: sqlite3.Connection, source: str, key: str, value: dict | None) -> None:
    """Store (or with ``value=None`` delete) a plugin checkpoint. Does not commit.

    The caller commits it together with the items it describes; see ``SyncCheckpoint``.
    """
    if value is None:
        conn.execute("DELETE FROM sync_checkpoints WHERE source = ? AND key = ?", (source, key))
        return
    conn.execute(
        """
        INSERT INTO sync_checkpoints (source, key, value, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(source, key) DO UPDATE SET
            value = excluded.value, updated_at = excluded.updated_at
        """,
        (source, key, json.dumps(value)),
    )


def get_checkpoints(conn: sqlite3.Connection, source: str) -> dict[str, dict]:
    """All checkpoints a plugin has saved, by key."""
    rows = conn.execute(
        "SELECT key, value FROM sync_checkpoints WHERE source = ?", (source,)
    ).fetchall()
    return {row[0]: json.loads(row[1]) for row in rows}


def add_tag(conn: sqlite3.Connection, item_id: int, tag_name: str) -> int:
    """Add a tag to an item. Creates the tag if it doesn't exist.

    Returns:
        The tag ID.
    """
    tag_name = tag_name.strip().lower()
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    tag_id = row[0]
    conn.execute(
        "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
        (item_id, tag_id),
    )
    conn.commit()
    return tag_id


def remove_tag(conn: sqlite3.Connection, item_id: int, tag_name: str) -> bool:
    """Remove a tag from an item.

    Returns:
        True if the tag was removed, False if it wasn't present.
    """
    tag_name = tag_name.strip().lower()
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    if not row:
        return False
    cursor = conn.execute(
        "DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?",
        (item_id, row[0]),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_tags(conn: sqlite3.Connection, item_id: int) -> list[str]:
    """Get all tag names for an item."""
    rows = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        WHERE it.item_id = ?
        ORDER BY t.name
        """,
        (item_id,),
    ).fetchall()
    return [row[0] for row in rows]


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """List all tags with their item counts."""
    rows = conn.execute(
        """
        SELECT t.name, COUNT(it.item_id) as count
        FROM tags t
        LEFT JOIN item_tags it ON t.id = it.tag_id
        GROUP BY t.id, t.name
        ORDER BY count DESC, t.name
        """,
    ).fetchall()
    return [{"name": row[0], "count": row[1]} for row in rows]


def get_cache_dir(config: Config | None = None) -> Path:
    """Get the cache directory for storing files like screenshots.

    Creates the directory if it doesn't exist.

    Returns:
        Path to ~/.lestash/cache/posts/
    """
    if config is None:
        config = Config.load()
    # Use the database path's parent as the base for cache
    db_path = Path(config.general.database_path).expanduser()
    cache_dir = db_path.parent / "cache" / "posts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_post_cache(conn: sqlite3.Connection, urn: str) -> dict | None:
    """Look up cached post content by URN.

    Args:
        conn: Database connection
        urn: LinkedIn activity URN (e.g., "urn:li:activity:7421941760257556482")

    Returns:
        Dict with content_preview, author_name, reactor_name, image_path, etc. or None if not found
    """
    cursor = conn.execute(
        """SELECT urn, author_urn, author_name, content_preview, full_content,
                  image_path, url, created_at, fetched_at, source, reactor_name
           FROM post_cache WHERE urn = ?""",
        (urn,),
    )
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def upsert_post_cache(
    conn: sqlite3.Connection,
    urn: str,
    content_preview: str | None = None,
    full_content: str | None = None,
    author_urn: str | None = None,
    author_name: str | None = None,
    image_path: str | None = None,
    url: str | None = None,
    source: str = "manual",
    reactor_name: str | None = None,
) -> None:
    """Add or update cached post content.

    Args:
        conn: Database connection
        urn: LinkedIn activity URN
        content_preview: First ~500 chars of post content
        full_content: Full post text if available
        author_urn: Author's LinkedIn URN
        author_name: Author's display name
        image_path: Path to screenshot (relative to cache dir)
        url: LinkedIn URL to the post
        source: How the content was obtained (manual, own_post, image, api)
        reactor_name: Name of the person who reacted (for reactions to your content)
    """
    conn.execute(
        """
        INSERT INTO post_cache (urn, author_urn, author_name, content_preview,
                                full_content, image_path, url, source, reactor_name, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(urn) DO UPDATE SET
            author_urn = COALESCE(excluded.author_urn, author_urn),
            author_name = COALESCE(excluded.author_name, author_name),
            content_preview = COALESCE(excluded.content_preview, content_preview),
            full_content = COALESCE(excluded.full_content, full_content),
            image_path = COALESCE(excluded.image_path, image_path),
            url = COALESCE(excluded.url, url),
            source = excluded.source,
            reactor_name = COALESCE(excluded.reactor_name, reactor_name),
            fetched_at = CURRENT_TIMESTAMP
        """,
        (
            urn,
            author_urn,
            author_name,
            content_preview,
            full_content,
            image_path,
            url,
            source,
            reactor_name,
        ),
    )
    conn.commit()


def get_media_dir(config: Config | None = None) -> Path:
    """Get the media directory for storing attachments.

    Returns:
        Path to ~/.lestash/media/
    """
    if config is None:
        config = Config.load()
    db_path = Path(config.general.database_path).expanduser()
    media_dir = db_path.parent / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def save_media_file(
    item_id: int,
    data: bytes,
    filename: str,
    config: Config | None = None,
) -> str:
    """Save media file to disk.

    Args:
        item_id: The item this media belongs to.
        data: File content bytes.
        filename: Original filename (used for extension).
        config: Optional config override.

    Returns:
        Relative path from media dir (e.g. "42/a1b2c3d4.jpg").
    """
    import hashlib

    media_dir = get_media_dir(config)
    item_dir = media_dir / str(item_id)

    ext = Path(filename).suffix or ".bin"
    file_hash = hashlib.sha256(data).hexdigest()[:12]
    rel_path = f"{item_id}/{file_hash}{ext}"

    dest = media_dir / rel_path
    if not dest.exists():
        for attempt in (1, 2):
            item_dir.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(data)
                break
            except FileNotFoundError:
                if attempt == 2:  # a prune removes empty item folders (#217)
                    raise
        logger.debug(f"Saved media file: {rel_path} ({len(data)} bytes)")

    return rel_path


def add_item_media(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    media_type: str,
    url: str | None = None,
    local_path: str | None = None,
    mime_type: str | None = None,
    alt_text: str | None = None,
    position: int = 0,
    source_origin: str = "sync",
    sha256: str | None = None,
    size: int | None = None,
    seen_at: str | None = None,
    superseded_at: str | None = None,
    _commit: bool = True,
) -> int:
    """Add a media attachment to an item.

    Skips insert if an identical (item_id, media_type, url, local_path, sha256) row
    exists. ``sha256`` names a file already in the file store, of ``size`` bytes.

    With ``sha256`` and ``seen_at``, the file is one version of the media at this
    position, from this ``source_origin``: see _put_media_version. A source that knows
    each file's versions (the Kobo's snapshots) gives them all, every time.

    Returns:
        The media row ID.
    """
    if sha256 and seen_at is not None:
        return _put_media_version(
            conn,
            item_id,
            media_type=media_type,
            mime_type=mime_type,
            alt_text=alt_text,
            position=position,
            source_origin=source_origin,
            sha256=sha256,
            size=size,
            seen_at=seen_at,
            superseded_at=superseded_at,
            commit=_commit,
        )
    if sha256:
        # A stored file is the same media at the same place: two identical slides in
        # one deck are two rows
        existing = conn.execute(
            "SELECT id FROM item_media WHERE item_id = ? AND media_type = ? "
            "AND sha256 = ? AND position = ?",
            (item_id, media_type, sha256, position),
        ).fetchone()
    else:
        # sha256 is set later on a file the store adopts, so it isn't compared here
        existing = conn.execute(
            """SELECT id FROM item_media
               WHERE item_id = ? AND media_type = ?
                 AND (url = ? OR (url IS NULL AND ? IS NULL))
                 AND (local_path = ? OR (local_path IS NULL AND ? IS NULL))""",
            (item_id, media_type, url, url, local_path, local_path),
        ).fetchone()
    if existing:
        return existing[0]

    if sha256:
        conn.execute(
            "INSERT OR IGNORE INTO blobs (sha256, size, mime_type) VALUES (?, ?, ?)",
            (sha256, size or 0, mime_type),
        )
    cursor = conn.execute(
        """INSERT INTO item_media
           (item_id, media_type, url, local_path, mime_type, alt_text, position, source_origin,
            sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            media_type,
            url,
            local_path,
            mime_type,
            alt_text,
            position,
            source_origin,
            sha256,
        ),
    )
    if _commit:
        conn.commit()
    return cursor.lastrowid or 0


def _put_media_version(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    media_type: str,
    mime_type: str | None,
    alt_text: str | None,
    position: int,
    source_origin: str,
    sha256: str,
    size: int | None,
    seen_at: str,
    superseded_at: str | None,
    commit: bool,
) -> int:
    """One version of the media at (item, type, position), from one origin.

    The row is the one of this file first seen at ``seen_at`` (kept in created_at).
    Its type and label are written when they differ, so an unchanged import writes
    nothing. A replaced version stays replaced: superseded_at is only ever set on a
    current row, so a source that forgets a snapshot (and with it, that a version was
    replaced) never makes two rows current; and the current one closes any other
    current row of the slot, a version the source no longer knows. A new row is
    numbered after the slot's
    others; numbers never change. Rows of another origin (an upload at the same
    position) are never matched.

    Else a current row of this file, or one replaced at the same time, is the one, and
    takes ``seen_at``: a row from before versions (an adopted file, created when it
    was synced, perhaps since redrawn), or one whose first sighting the source forgot.
    """
    conn.execute(
        "INSERT OR IGNORE INTO blobs (sha256, size, mime_type) VALUES (?, ?, ?)",
        (sha256, size or 0, mime_type),
    )
    slot = "item_id = ? AND media_type = ? AND position = ? AND source_origin = ?"
    key = (item_id, media_type, position, source_origin)
    row = conn.execute(
        f"SELECT id FROM item_media WHERE {slot} AND sha256 = ? AND created_at = ? "
        "ORDER BY id LIMIT 1",
        (*key, sha256, seen_at),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT id FROM item_media WHERE {slot} AND sha256 = ? "
            "AND (superseded_at IS NULL OR superseded_at IS ?) ORDER BY version, id LIMIT 1",
            (*key, sha256, superseded_at),
        ).fetchone()
        if row is not None:
            conn.execute("UPDATE item_media SET created_at = ? WHERE id = ?", (seen_at, row[0]))
    # The source's current file. A replaced row of it means the file came back after a
    # version the source no longer knows: its current row, else a new one.
    if (
        superseded_at is None
        and row is not None
        and conn.execute(
            "SELECT superseded_at IS NOT NULL FROM item_media WHERE id = ?", (row[0],)
        ).fetchone()[0]
    ):
        row = conn.execute(
            f"SELECT id FROM item_media WHERE {slot} AND sha256 = ? "
            "AND superseded_at IS NULL ORDER BY version DESC, id DESC LIMIT 1",
            (*key, sha256),
        ).fetchone()
    if row:
        conn.execute(
            """UPDATE item_media
               SET mime_type = ?, alt_text = ?, superseded_at = coalesce(superseded_at, ?)
               WHERE id = ? AND (mime_type IS NOT ? OR alt_text IS NOT ?
                     OR (superseded_at IS NULL AND ? IS NOT NULL))""",
            (mime_type, alt_text, superseded_at, row[0], mime_type, alt_text, superseded_at),
        )
        media_id = row[0]
    else:
        (last,) = conn.execute(
            f"SELECT coalesce(max(version), 0) FROM item_media WHERE {slot}", key
        ).fetchone()
        cursor = conn.execute(
            """INSERT INTO item_media
               (item_id, media_type, mime_type, alt_text, position, source_origin,
                sha256, version, superseded_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                media_type,
                mime_type,
                alt_text,
                position,
                source_origin,
                sha256,
                last + 1,
                superseded_at,
                seen_at,
            ),
        )
        media_id = cursor.lastrowid or 0
    if superseded_at is None:
        # One current row: any other is a version the source no longer knows (its
        # snapshot or blob lost), replaced when this one was seen, or when it was itself
        # first seen if that is later (a file come back keeps its first sighting)
        conn.execute(
            f"UPDATE item_media SET superseded_at = max(?, coalesce(created_at, ?)) "
            f"WHERE {slot} AND superseded_at IS NULL AND id != ? AND sha256 IS NOT NULL",
            (seen_at, seen_at, *key, media_id),
        )
    if commit:
        conn.commit()
    return media_id


def get_item_media(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """Get all media attachments for an item, older versions too, ordered by
    position, then version."""
    rows = conn.execute(
        """SELECT id, item_id, media_type, url, local_path, mime_type,
                  alt_text, position, source_origin, created_at, sha256,
                  version, superseded_at
           FROM item_media WHERE item_id = ? ORDER BY position, version, id""",
        (item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_item_media_batch(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[dict]]:
    """Batch-fetch the current media of several items (no older versions), for
    listings. Returns {item_id: [media_dicts]}."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"""SELECT id, item_id, media_type, url, local_path, mime_type,
                   alt_text, position, source_origin, created_at, sha256
            FROM item_media WHERE item_id IN ({placeholders}) AND superseded_at IS NULL
            ORDER BY item_id, position""",
        item_ids,
    ).fetchall()
    result: dict[int, list[dict]] = {iid: [] for iid in item_ids}
    for row in rows:
        d = dict(row)
        result[d["item_id"]].append(d)
    return result


def delete_item_media(conn: sqlite3.Connection, media_id: int) -> bool:
    """Delete a media attachment by ID. Returns True if deleted."""
    cursor = conn.execute("DELETE FROM item_media WHERE id = ?", (media_id,))
    conn.commit()
    return cursor.rowcount > 0


# --- syndications (Wave 2a slice 3) ---


def get_syndication(conn: sqlite3.Connection, item_id: int, target: str) -> dict | None:
    """Return the most recent syndication for (item, target), or None."""
    row = conn.execute(
        """SELECT id, item_id, target, target_url, published_at,
                  request_body, response_body
           FROM syndications
           WHERE item_id = ? AND target = ?
           ORDER BY published_at DESC LIMIT 1""",
        (item_id, target),
    ).fetchone()
    return dict(row) if row else None


def record_syndication(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    target: str,
    target_url: str,
    request_body: str | None = None,
    response_body: str | None = None,
) -> int:
    """Insert a syndication audit row. Returns the new row id.

    Caller is expected to JSON-serialise request_body / response_body if they
    want structured data persisted. The columns are TEXT so anything else
    works too (e.g. provider returned plain text).
    """
    cursor = conn.execute(
        """INSERT INTO syndications
           (item_id, target, target_url, request_body, response_body)
           VALUES (?, ?, ?, ?, ?)""",
        (item_id, target, target_url, request_body, response_body),
    )
    conn.commit()
    rid = cursor.lastrowid
    assert rid is not None
    return rid

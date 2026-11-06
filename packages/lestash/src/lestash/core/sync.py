"""Store what a source plugin's ``sync()`` yields. Shared by the CLI and the server."""

import json
import logging
import sqlite3
from collections.abc import Iterable

from lestash.core.database import (
    add_item_media,
    resolve_parent_id,
    resolve_parent_source_ids,
    save_checkpoint,
)
from lestash.importers import merge_with_stored
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint

logger = logging.getLogger(__name__)

# Commit this often, so a long sync (a Takeout zip takes minutes) doesn't hold the write
# lock throughout. Safe: a re-import upserts and merges, so items committed before their
# checkpoint are only imported again.
COMMIT_EVERY = 500


class SyncStore:
    """Writes a sync's items and checkpoints, and counts what it wrote.

    Read ``items``, ``source_types`` and ``linked`` after ``run``, also when it raised.
    """

    def __init__(self, conn: sqlite3.Connection, source: str) -> None:
        self.conn, self.source = conn, source
        self.items = 0
        self.source_types: set[str] = set()
        self.linked = 0

    def run(self, items: Iterable[ItemCreate | SyncCheckpoint]) -> None:
        """Store every item, then link children to parents yielded later.

        If the plugin raises, what was stored is committed and linked, then the error
        propagates. A ``KeyboardInterrupt`` commits nothing more.
        """
        try:
            for item in items:
                if isinstance(item, SyncCheckpoint):
                    # Commit the plugin's state with the items yielded before it
                    save_checkpoint(self.conn, self.source, item.key, item.value)
                    self.conn.commit()
                    continue
                self._store(item)
                if self.items % COMMIT_EVERY == 0:
                    self.conn.commit()
        except Exception:
            self.conn.commit()
            try:
                self._sweep()
            except Exception:
                logger.exception("Linking parents failed after a failed %s sync", self.source)
            raise
        self.conn.commit()
        self._sweep()

    def _sweep(self) -> None:
        self.linked = resolve_parent_source_ids(self.conn, self.source_types)

    def _store(self, item: ItemCreate) -> None:
        # Link to a parent yielded earlier (metadata._parent_source_id)
        resolve_parent_id(self.conn, item)
        merge_with_stored(self.conn, item)
        metadata_json = json.dumps(item.metadata) if item.metadata else None
        (item_id,) = self.conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                content = excluded.content,
                author = excluded.author,
                is_own_content = excluded.is_own_content,
                metadata = excluded.metadata,
                -- Keep a parent set earlier (e.g. by a resolver) when the
                -- plugin doesn't name one
                parent_id = COALESCE(excluded.parent_id, items.parent_id)
            RETURNING id
            """,
            (
                item.source_type,
                item.source_id,
                item.url,
                item.title,
                item.content,
                item.author,
                item.created_at,
                item.is_own_content,
                metadata_json,
                item.parent_id,
            ),
        ).fetchone()
        self.items += 1
        self.source_types.add(item.source_type)
        for media in item.media or []:
            add_item_media(
                self.conn,
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

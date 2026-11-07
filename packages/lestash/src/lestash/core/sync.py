"""Run a source plugin's sync and store what it yields. Shared by the CLI and the server."""

import fcntl
import json
import logging
import signal
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Self

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
        self.committed = 0  # items as of the last commit
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
                    self._commit()
                    continue
                self._store(item)
                if self.items % COMMIT_EVERY == 0:
                    self._commit()
        except Exception:
            self._commit()
            try:
                self._sweep()
            except Exception:
                logger.exception("Linking parents failed after a failed %s sync", self.source)
            raise
        self._commit()
        self._sweep()

    def _commit(self) -> None:
        self.conn.commit()
        self.committed = self.items

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


INTERRUPTED = "interrupted"


class SyncAlreadyRunning(RuntimeError):
    """Another sync of the same source holds its lock."""


class SyncRun:
    """One sync's row in ``sync_log``, held under a per-source lock.

    The lock is an ``flock`` on a file beside the database. The kernel releases it when
    the process ends, however it ends: SIGKILL and the OOM killer included. So a ``running``
    row found while holding the lock belongs to a sync that died: ``start`` marks it
    ``failed``, with the error ``interrupted``.

    Use as a context manager and call ``finish``. Leaving without ``finish`` records the
    run as ``failed``: ``interrupted`` for Ctrl-C or SIGTERM, else the exception's text.
    """

    def __init__(
        self, conn: sqlite3.Connection, source: str, sync_id: int, lock: IO[bytes] | None
    ) -> None:
        self.conn, self.source, self.id, self._lock = conn, source, sync_id, lock
        self.store: SyncStore | None = None  # counts items_added when not given
        self.finished = False

    @classmethod
    def start(
        cls,
        conn: sqlite3.Connection,
        source: str,
        store: SyncStore | None = None,
        wait: Callable[[], None] | None = None,
    ) -> Self:
        """Take the source's lock, close out dead runs, and log this one as ``running``.

        If another sync holds the lock, wait for it when ``wait`` is given (it is called
        once, before waiting, so the caller can say so); otherwise raise.

        Raises:
            SyncAlreadyRunning: another process or thread is syncing ``source``.
        """
        lock = _take_lock(conn, source, wait)
        try:
            if lock is not None:
                conn.execute(
                    "UPDATE sync_log SET status = 'failed', error_message = ? "
                    "WHERE source_type = ? AND status = 'running'",
                    (INTERRUPTED, source),
                )
            sync_id = conn.execute(
                "INSERT INTO sync_log (source_type, started_at, status) VALUES (?, ?, ?)",
                (source, _now(), "running"),
            ).lastrowid
            conn.commit()
        except BaseException:
            if lock is not None:
                lock.close()
            raise
        assert sync_id is not None
        run = cls(conn, source, sync_id, lock)
        run.store = store
        return run

    def finish(
        self,
        status: str,
        items_added: int | None = None,
        items_updated: int = 0,
        error: str | None = None,
    ) -> None:
        """Record how the run ended. Only the first call counts."""
        if self.finished:
            return
        if items_added is None:
            items_added = self.store.items if self.store else 0
        self.conn.execute(
            """
            UPDATE sync_log SET completed_at = ?, status = ?, items_added = ?,
                items_updated = ?, error_message = ?
            WHERE id = ?
            """,
            (_now(), status, items_added, items_updated, error, self.id),
        )
        self.conn.commit()
        self.finished = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if not self.finished:
                if exc is None:
                    error = "ended without a status"
                elif isinstance(exc, KeyboardInterrupt | SystemExit):
                    error = INTERRUPTED
                else:
                    error = str(exc) or type(exc).__name__
                # Drop what was stored since the last commit, and count only what stays
                self.conn.rollback()
                kept = self.store.committed if self.store else None
                self.finish("failed", items_added=kept, error=error)
        finally:
            if self._lock is not None:
                self._lock.close()


def _now() -> str:
    # The format sqlite3's deprecated default adapter wrote, which the rows already use
    return datetime.now().isoformat(" ")


def _take_lock(
    conn: sqlite3.Connection, source: str, wait: Callable[[], None] | None
) -> IO[bytes] | None:
    """Lock ``<database>.sync-<source>.lock``; None for an in-memory database."""
    db_file = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not db_file:
        return None
    # Kept open for the whole run: closing the file releases the lock
    lock = Path(f"{db_file}.sync-{source}.lock").open("ab")  # noqa: SIM115
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if wait is None:
            lock.close()
            raise SyncAlreadyRunning(f"a {source} sync is already running") from None
        wait()
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except BaseException:
            lock.close()
            raise
    return lock


@contextmanager
def sigterm_as_exit() -> Iterator[None]:
    """Turn SIGTERM into ``SystemExit``, so ``SyncRun`` logs a stopped sync.

    systemd stops a unit with SIGTERM, whose default action ends Python at once.
    Only the main thread can set a handler; elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _exit(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _exit)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)

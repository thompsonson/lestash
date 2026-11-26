"""Run a source plugin's sync and store what it yields. Shared by the CLI and the server."""

import fcntl
import json
import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Final, Self

from lestash.core import paths
from lestash.core.activities import record_activities
from lestash.core.agents import link_agents
from lestash.core.database import (
    add_item_media,
    begin_write,
    resolve_parent_id,
    resolve_parent_source_ids,
    save_checkpoint,
    write_transaction,
)
from lestash.core.extract import content_on_import
from lestash.core.import_reminders import record_import
from lestash.core.links import find_links, store_links
from lestash.core.pdfs import link_copies
from lestash.importers import merge_with_stored
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint

logger = logging.getLogger(__name__)

# A sync gathers what the plugin yields with no write open, then writes it in one short
# transaction once this many items wait, or when an item comes after the first has
# waited FLUSH_AFTER_S (a plugin's fetch is never cut short). So the write lock is never
# held while a plugin fetches (#401). Safe: a re-import upserts
# and merges, so items written before their checkpoint are only imported again.
COMMIT_EVERY = 500
FLUSH_AFTER_S = 5.0
# A batch whose write holds the lock longer than this is logged, to show which work
# inside it is slow
SLOW_WRITE_S = 2.0


@dataclass(frozen=True)
class SyncCounts:
    """What a sync wrote: new items, changed items, and items yielded again unchanged."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0


# Insert an item, or update its stored row only when a column would change, so an
# unchanged re-import writes nothing: no FTS reindex and no item_history row. A plugin
# naming no parent keeps one set earlier (e.g. by a resolver). created_at is kept.
# RETURNING gives the id of a row written; none comes back for an unchanged one.
_UPSERT = """
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
        parent_id = COALESCE(excluded.parent_id, items.parent_id)
    WHERE (
        items.url, items.title, items.content, items.author,
        items.is_own_content, items.metadata, items.parent_id
    ) IS NOT (
        excluded.url, excluded.title, excluded.content, excluded.author,
        excluded.is_own_content, excluded.metadata,
        COALESCE(excluded.parent_id, items.parent_id)
    )
    RETURNING id
"""


def dedupe_notebooks(conn: sqlite3.Connection, source: str) -> None:
    """Merge NotebookLM notebooks held twice (see notebooklm_dedupe). A failure leaves
    the items stored, and the next import tries again."""
    from lestash.importers.notebooklm_dedupe import dedupe_notebooklm

    try:
        if pairs := dedupe_notebooklm(conn):
            logger.info("Merged %d NotebookLM notebook copies after %s", len(pairs), source)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Merging NotebookLM notebook copies failed after %s", source)


class SyncStore:
    """Writes a sync's items and checkpoints, and counts what it wrote.

    Read ``counts``, ``source_types`` and ``linked`` after ``run``, also when it raised.
    """

    def __init__(self, conn: sqlite3.Connection, source: str) -> None:
        self.conn, self.source = conn, source
        self.added = self.updated = self.unchanged = 0
        self.committed = SyncCounts()  # counts as of the last commit
        self.source_types: set[str] = set()
        self.linked = 0
        self.wrote_notebooks = False  # whether a NotebookLM notebook was stored

    @property
    def counts(self) -> SyncCounts:
        return SyncCounts(self.added, self.updated, self.unchanged)

    @property
    def items(self) -> int:
        """Items stored: new, changed or unchanged. One yielded twice counts twice."""
        return self.added + self.updated + self.unchanged

    def run(self, items: Iterable[ItemCreate | SyncCheckpoint]) -> None:
        """Store every item, then link children to parents yielded later.

        Items wait in memory while the plugin works, and are written in batches (see
        ``COMMIT_EVERY``), so no write is open while the plugin fetches. If the plugin
        raises, what it yielded is stored and linked, then the error propagates. A
        ``KeyboardInterrupt`` stores nothing more.

        Every way out takes the same steps: store what is pending, link, then raise the
        first error. Linking is skipped only when the store's write lock couldn't be had
        and nothing was stored, as it would only wait out the lock again.
        """
        pending: list[ItemCreate] = []
        first_at = 0.0  # when the oldest pending item came
        errors: list[Exception] = []
        try:
            for item in items:
                if isinstance(item, SyncCheckpoint):
                    # The plugin's state is written with the items yielded before it
                    self._write(pending, item)
                    continue
                if not pending:
                    first_at = time.monotonic()
                pending.append(item)
                if len(pending) >= COMMIT_EVERY or time.monotonic() - first_at >= FLUSH_AFTER_S:
                    self._write(pending)
        except Exception as e:
            errors.append(e)
        try:
            self._write(pending)  # nothing, after a write that failed: it empties pending
        except Exception as e:
            errors.append(e)
        lock_not_had = any(isinstance(e, _LockNotHad) for e in errors)
        if not lock_not_had or self.committed.added or self.committed.updated:
            try:
                self._sweep()
            except Exception as e:
                errors.append(e)
        for later in errors[1:]:
            logger.error("Also failed in the %s sync", self.source, exc_info=later)
        if errors:
            raise errors[0]

    def _write(self, pending: list[ItemCreate], checkpoint: SyncCheckpoint | None = None) -> None:
        """Store the pending items, and the checkpoint after them, in one transaction.

        Empties ``pending``. If an item fails, what was stored before it is committed and
        the rest are dropped, as the error ends the sync.
        """
        if not pending and checkpoint is None:
            return
        try:
            # Inside the try: if the lock can't be had, the items are dropped as the
            # error ends the sync, and not waited for again
            try:
                begin_write(self.conn)
            except sqlite3.OperationalError as e:
                if "locked" in str(e):
                    raise _LockNotHad(str(e)) from e
                raise
            began = time.monotonic()
            for item in pending:
                self._store(item)
            if checkpoint is not None:
                save_checkpoint(self.conn, self.source, checkpoint.key, checkpoint.value)
        except Exception:
            self._commit()
            raise
        finally:
            count = len(pending)
            pending.clear()
        self._commit()
        if (held := time.monotonic() - began) > SLOW_WRITE_S:
            logger.warning(
                "Writing %d %s items held the write lock for %.1f s", count, self.source, held
            )

    def _commit(self) -> None:
        self.conn.commit()
        self.committed = self.counts

    def _sweep(self) -> None:
        self.linked = resolve_parent_source_ids(self.conn, self.source_types)
        counts = self.counts
        if counts.added or counts.updated:
            # A written item can link another, or be the item another links to. Links
            # are derived: a failure leaves the items stored, and the next sync catches up.
            self.conn.commit()  # keep the parents just linked if linking fails
            if self.wrote_notebooks:
                dedupe_notebooks(self.conn, self.source)
            try:
                # Found before the write: finding reads every item (#401)
                links = list(find_links(self.conn))
                with write_transaction(self.conn):
                    store_links(self.conn, links)
            except Exception:
                logger.exception("Linking items failed after the %s sync", self.source)
            try:
                link_agents(self.conn)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("Recording agents failed after the %s sync", self.source)
            try:
                link_copies(self.conn)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("Linking copies of PDFs failed after the %s sync", self.source)

    def _store(self, item: ItemCreate) -> bool:
        """Store one item; False when merge_with_stored() says a stored item holds it.

        Counts it as added (no stored row), updated (a column or new media) or
        unchanged. A merge's own fix-ups to the stored row aren't counted.
        """
        # Link to a parent yielded earlier (metadata._parent_source_id)
        resolve_parent_id(self.conn, item)
        if not merge_with_stored(self.conn, item):
            return False
        if item.source_type == "notebooklm" and (item.metadata or {}).get("type") == "notebook":
            self.wrote_notebooks = True
        stored = self.conn.execute(
            "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
            (item.source_type, item.source_id),
        ).fetchone()
        # Text read from its files (#218) is kept, so an import adds no history
        with_text = content_on_import(
            self.conn,
            item.source_type,
            item.content,
            item.metadata,
            item.media or [],
            stored[0] if stored else None,
        )
        if with_text is not None:
            item = item.model_copy(update={"content": with_text[0], "metadata": with_text[1]})
        # A row back means the upsert wrote one; none, that the stored row is the same
        written = self.conn.execute(
            _UPSERT,
            (
                item.source_type,
                item.source_id,
                item.url,
                item.title,
                item.content,
                item.author,
                item.created_at,
                item.is_own_content,
                json.dumps(item.metadata) if item.metadata else None,
                item.parent_id,
            ),
        ).fetchall()
        if written:
            item_id = written[0][0]
        else:
            # Unchanged. If another writer stored the same item since the check above,
            # read its id now (and count it unchanged), rather than fail the sync.
            stored = (
                stored
                or self.conn.execute(
                    "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
                    (item.source_type, item.source_id),
                ).fetchone()
            )
            item_id = stored[0]
        record_activities(self.conn, item_id, item.activities)
        self.source_types.add(item.source_type)
        # item_media has no FTS index, so this count isn't moved by FTS5 flushing its
        # buffered writes, as total_changes around the upsert would be
        changes_before_media = self.conn.total_changes
        try:
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
                    sha256=media.sha256,
                    size=media.size,
                    seen_at=media.seen_at,
                    superseded_at=media.superseded_at,
                    _commit=False,
                )
        finally:
            # Counted even when a media insert fails: run() commits the item row
            if stored is None:
                self.added += 1
            elif written or self.conn.total_changes > changes_before_media:
                self.updated += 1
            else:
                self.unchanged += 1
        return True


class _LockNotHad(sqlite3.OperationalError):
    """The store's own write couldn't take the lock, even after begin_write's retry.

    Its own type, so a "locked" error the plugin raises isn't taken for one.
    """


INTERRUPTED = "interrupted"


# Syncs that read an export you asked for, and the import kind each counts as
SYNCED_IMPORTS: Final[Mapping[str, str]] = {"takeout": "takeout"}


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
        self.store: SyncStore | None = None  # gives the counts finish() is not given
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
        items_updated: int | None = None,
        error: str | None = None,
    ) -> None:
        """Record how the run ended. Only the first call counts.

        Counts not given come from the store: new items as added, changed items as
        updated. Items stored again unchanged are in neither.
        """
        if self.finished:
            return
        counts = self.store.counts if self.store else SyncCounts()
        if items_added is None:
            items_added = counts.added
        if items_updated is None:
            items_updated = counts.updated
        if status == "completed" and items_added and self.source in SYNCED_IMPORTS:
            # A Takeout the sync read from Drive is an import you asked for (#403)
            record_import(self.conn, SYNCED_IMPORTS[self.source], items_added)
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
                kept = self.store.committed if self.store else SyncCounts()
                self.finish(
                    "failed", items_added=kept.added, items_updated=kept.updated, error=error
                )
        finally:
            if self._lock is not None:
                self._lock.close()


def _now() -> str:
    # The format sqlite3's deprecated default adapter wrote, which the rows already use
    return datetime.now().isoformat(" ")


def _take_lock(
    conn: sqlite3.Connection, source: str, wait: Callable[[], None] | None
) -> IO[bytes] | None:
    """Lock the source's sync; None for an in-memory database.

    The lock file is ``sync-<source>.lock`` in ``paths.locks_dir()`` for the default
    database, and ``<database>.sync-<source>.lock`` beside any other (#304).
    """
    db_file = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not db_file:
        return None
    if Path(db_file).resolve() == paths.database_path().resolve():
        locks = paths.locks_dir()
        locks.mkdir(parents=True, exist_ok=True)
        lock_path = locks / f"sync-{source}.lock"
    else:
        lock_path = Path(f"{db_file}.sync-{source}.lock")
    # Kept open for the whole run: closing the file releases the lock
    lock = lock_path.open("ab")  # noqa: SIM115
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

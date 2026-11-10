"""One store for every file LeStash keeps: each stored once, named by its SHA-256.

See #217 (W2). Files live at ``<root>/blobs/ab/abcdef…``; the ``blobs`` table records
each one's size and type, and ``item_media.sha256`` points a media row at its file.

The root is ``~/.local/share/lestash`` (backed up with ``~/.local``) for the default
database, and a ``store`` folder beside any other database, so a test environment
never writes into the real store. ``general.store_path`` in the config overrides it.

A file is written to a temporary name and renamed when complete, so an interrupted
copy never leaves a half-written blob.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from lestash.core.config import Config, get_database_path

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "~/.local/share/lestash"
CHUNK = 1 << 20


def store_root(config: Config | None = None) -> Path:
    """The store's folder for this config's database."""
    config = config or Config.load()
    if config.general.store_path:
        return Path(config.general.store_path).expanduser()
    db_path = Path(config.general.database_path).expanduser()
    if db_path.resolve() == get_database_path().resolve():
        return Path(DEFAULT_ROOT).expanduser()
    return db_path.parent / "store"


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Blob:
    sha256: str
    size: int
    new: bool  # whether this put stored it; False if it was already there


class BlobStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.blobs = self.root / "blobs"

    def path(self, sha256: str) -> Path:
        return self.blobs / sha256[:2] / sha256

    def has(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def put_file(self, src: Path) -> Blob:
        """Store a copy of the file at ``src``; one already stored is not copied again."""
        sha256 = sha_file(src)
        dest = self.path(sha256)
        if dest.is_file():
            return Blob(sha256, dest.stat().st_size, new=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{sha256[:12]}-", dir=dest.parent)
        os.close(fd)
        try:
            shutil.copyfile(src, tmp_name)
            if sha_file(Path(tmp_name)) != sha256:
                raise OSError(f"{src} changed while it was copied")
            size = Path(tmp_name).stat().st_size  # of what was stored, not of src now
            os.chmod(tmp_name, 0o644)  # mkstemp makes it 0600
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return Blob(sha256, size, new=True)

    def put_stream(self, stream: IO[bytes]) -> Blob:
        """Store what ``stream`` reads, such as a zip member; one already stored is kept."""
        self.blobs.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=self.blobs)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            sha256 = digest.hexdigest()
            dest = self.path(sha256)
            if dest.is_file():
                Path(tmp_name).unlink()
                return Blob(sha256, dest.stat().st_size, new=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return Blob(sha256, size, new=True)


def record_blob(conn: sqlite3.Connection, blob: Blob, mime_type: str | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO blobs (sha256, size, mime_type) VALUES (?, ?, ?)",
        (blob.sha256, blob.size, mime_type),
    )


@dataclass
class AdoptCounts:
    adopted: int = 0  # media rows now pointing at the store
    new_blobs: int = 0
    new_bytes: int = 0
    missing: int = 0  # rows whose local file is gone
    failed: int = 0  # rows whose file could not be read or copied; tried again next time


def adopt_media(
    conn: sqlite3.Connection, config: Config | None = None, ids: list[int] | None = None
) -> AdoptCounts:
    """Copy each media file not yet in the store into it, and point its row at it; only
    the rows ``ids`` if given. The file in the media folder stays until
    ``prune_media`` checks the stored copy. Safe to run again.

    The files are copied first, with no write open, then the rows are written and
    committed in one short transaction: a first run's backlog must not hold the write
    lock while it copies. A file that can't be read is counted and skipped, so one bad
    file never stops a sync. Call it with no transaction open.
    """
    from lestash.core.database import get_media_dir

    config = config or Config.load()
    store = BlobStore(store_root(config))
    media_dir = get_media_dir(config)
    counts = AdoptCounts()
    query = (
        "SELECT id, local_path, mime_type FROM item_media "
        "WHERE local_path IS NOT NULL AND sha256 IS NULL"
    )
    params: list[int] = []
    if ids is not None:
        query += f" AND id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    _clear_stale_shas(conn)
    rows = conn.execute(query + " ORDER BY id", params).fetchall()
    stored: list[tuple[int, Blob, str | None]] = []
    repaired: list[tuple[int, Blob, str | None]] = []
    for media_id, local_path, mime_type in rows:
        src = media_dir / local_path
        if not src.is_file():
            # Pruned while a writer recorded another row for the same file: another
            # row naming the path points at its blob
            sha256 = _stored_sha_for(conn, store, local_path)
            if sha256:
                size = store.path(sha256).stat().st_size
                repaired.append((media_id, Blob(sha256, size, new=False), mime_type))
            else:
                counts.missing += 1
            continue
        try:
            blob = store.put_file(src)
        except OSError as e:
            logger.warning("Could not store media file %s: %s", src, e)
            counts.failed += 1
            continue
        stored.append((media_id, blob, mime_type))
        if blob.new:
            counts.new_blobs += 1
            counts.new_bytes += blob.size
    if stored or repaired:
        with conn:
            for media_id, blob, mime_type in stored:
                record_blob(conn, blob, mime_type)
                conn.execute(
                    "UPDATE item_media SET sha256 = ? WHERE id = ? AND sha256 IS NULL",
                    (blob.sha256, media_id),
                )
            for media_id, blob, mime_type in repaired:
                record_blob(conn, blob, mime_type)  # a restored database may lack it
                conn.execute(
                    "UPDATE item_media SET sha256 = ? WHERE id = ? AND sha256 IS NULL",
                    (blob.sha256, media_id),
                )
        counts.adopted = len(stored) + len(repaired)
    return counts


def _clear_stale_shas(conn: sqlite3.Connection) -> None:
    """Clear a sha256 that is not the file its row names, so adopt stores the file.

    A media-folder file is named by the start of its hash (save_media_file). Before
    #217 step 4, ``set_source_pdf`` could move a row to another file and keep the old
    file's hash, and the store would serve the old file.
    """
    stale = [
        media_id
        for media_id, local_path, sha256 in conn.execute(
            "SELECT id, local_path, sha256 FROM item_media "
            "WHERE local_path IS NOT NULL AND sha256 IS NOT NULL"
        )
        if _hash_stem(local_path) and not sha256.startswith(_hash_stem(local_path) or "")
    ]
    if stale:
        logger.warning("Media rows naming another file than their hash: %s", stale)
        with conn:
            conn.executemany(
                "UPDATE item_media SET sha256 = NULL WHERE id = ?", [(i,) for i in stale]
            )


def _hash_stem(local_path: str) -> str | None:
    """The hash start a media-folder file is named by, if it is so named."""
    stem = Path(local_path).stem
    hexdigits = "0123456789abcdef"
    return stem if len(stem) == 12 and all(c in hexdigits for c in stem) else None


def _stored_sha_for(conn: sqlite3.Connection, store: BlobStore, local_path: str) -> str | None:
    """The stored file ``local_path`` was: the blob another row naming it points at,
    else the one blob whose hash starts with its name (``<sha256[:12]><ext>``, see
    save_media_file)."""
    stem = Path(local_path).stem
    found = {
        sha256
        for (sha256,) in conn.execute(
            "SELECT DISTINCT sha256 FROM item_media WHERE local_path = ? AND sha256 IS NOT NULL",
            (local_path,),
        )
    }
    if len(found) > 1:
        return None
    if found:
        sha256 = found.pop()
        return sha256 if sha256.startswith(stem) and store.has(sha256) else None
    # No row names it with a hash, as in a database restored from a backup taken
    # before the prune: the one stored file whose hash starts with the name
    if _hash_stem(local_path) is None:
        return None
    shard = store.blobs / stem[:2]
    matches = [p.name for p in shard.glob(f"{stem}*")] if shard.is_dir() else []
    if len(matches) != 1 or len(matches[0]) != 64:
        return None
    return matches[0] if sha_file(shard / matches[0]) == matches[0] else None


def media_file(conn_row: dict, config: Config | None = None) -> Path | None:
    """The file a media row names: its blob in the store, else its file in the media
    folder; None if neither is there."""
    sha256 = conn_row.get("sha256")
    if sha256:
        path = BlobStore(store_root(config)).path(sha256)
        if path.is_file():
            return path
    if conn_row.get("local_path"):
        from lestash.core.database import get_media_dir

        path = get_media_dir(config) / conn_row["local_path"]
        if path.is_file():
            return path
    return None


@dataclass
class PruneCounts:
    removed: int = 0  # media-folder copies removed, their files kept in the store
    removed_bytes: int = 0
    kept: int = 0  # copies kept: not yet stored, named by no row, or not matching
    shared_with: Path | None = None  # another database sharing the folder: none pruned
    stopped: bool = False  # the write lock was busy; the rest wait for the next run


def prune_media(
    conn: sqlite3.Connection, config: Config | None = None, *, delete: bool = True
) -> PruneCounts:
    """Remove each media-folder file whose copy in the store is checked (#217).

    A file goes only when every media row naming it points at one blob, that blob is
    in the store, and the file's own hash is that blob's. Anything else stays: a file
    no row names yet (a writer between saving and recording it), a row not yet
    adopted, or a file that differs from its row's blob. With ``delete=False`` it
    only counts. Empty item folders are removed. Safe to run again. Call it with no
    transaction open: each removal takes the write lock briefly. It does nothing when
    another database shares the media folder.
    """
    from lestash.core.database import get_media_dir

    config = config or Config.load()
    store = BlobStore(store_root(config))
    media_dir = get_media_dir(config)
    counts = PruneCounts()
    db_path = Path(config.general.database_path).expanduser()
    from lestash.core.db_admin import BACKUP_GLOB

    backups = set(db_path.parent.glob(BACKUP_GLOB))  # older copies of this database
    others = [p for p in db_path.parent.glob("*.db") if p.name != db_path.name and p not in backups]
    if others:
        # The media folder is <db dir>/media: another database there shares it, and
        # its rows are not ours to read
        logger.warning("Not pruning %s: shared with %s", media_dir, others[0])
        counts.shared_with = others[0]
        return counts
    shas: dict[str, set[str | None]] = {}
    for local_path, sha256 in conn.execute(
        "SELECT local_path, sha256 FROM item_media WHERE local_path IS NOT NULL"
    ):
        shas.setdefault(local_path, set()).add(sha256)
    for path in sorted(p for p in media_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(media_dir).as_posix()
        named = shas.get(rel, set())
        sha256 = next(iter(named)) if len(named) == 1 else None
        try:
            ok = (
                sha256 is not None
                and store.has(sha256)
                and sha_file(path) == sha256
                and sha_file(store.path(sha256)) == sha256  # the stored copy is whole
            )
            if ok and delete:
                # Checked again under the write lock: a writer may have recorded a
                # new row for this file since the rows were read
                conn.execute("BEGIN IMMEDIATE")
                try:
                    now = {
                        s
                        for (s,) in conn.execute(
                            "SELECT DISTINCT sha256 FROM item_media WHERE local_path = ?",
                            (rel,),
                        )
                    }
                    ok = now == {sha256}
                    if ok:
                        size = path.stat().st_size
                        path.unlink()
                finally:
                    conn.commit()
            elif ok:
                size = path.stat().st_size
            if ok:
                counts.removed += 1
                counts.removed_bytes += size
            else:
                counts.kept += 1
        except OSError as e:
            logger.warning("Could not prune media file %s: %s", path, e)
            counts.kept += 1
        except sqlite3.OperationalError as e:  # the lock is busy: the rest next time
            logger.warning("Stopped pruning media at %s: %s", path, e)
            counts.kept += 1
            counts.stopped = True
            break
    if delete:
        for folder in sorted((p for p in media_dir.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):  # not empty
                folder.rmdir()
    return counts

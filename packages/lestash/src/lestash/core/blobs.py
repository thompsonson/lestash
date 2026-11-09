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

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lestash.core.config import Config, get_database_path

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
        size = src.stat().st_size
        if dest.is_file() and dest.stat().st_size == size:
            return Blob(sha256, size, new=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{sha256[:12]}-", dir=dest.parent)
        os.close(fd)
        try:
            shutil.copyfile(src, tmp_name)
            if sha_file(Path(tmp_name)) != sha256:
                raise OSError(f"{src} changed while it was copied")
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


def adopt_media(conn: sqlite3.Connection, config: Config | None = None) -> AdoptCounts:
    """Copy each media file not yet in the store into it, and point its row at it.

    The file in the media folder stays. Safe to run again; the caller commits.
    """
    from lestash.core.database import get_media_dir

    config = config or Config.load()
    store = BlobStore(store_root(config))
    media_dir = get_media_dir(config)
    counts = AdoptCounts()
    rows = conn.execute(
        "SELECT id, local_path, mime_type FROM item_media "
        "WHERE local_path IS NOT NULL AND sha256 IS NULL ORDER BY id"
    ).fetchall()
    for media_id, local_path, mime_type in rows:
        src = media_dir / local_path
        if not src.is_file():
            counts.missing += 1
            continue
        blob = store.put_file(src)
        record_blob(conn, blob, mime_type)
        conn.execute("UPDATE item_media SET sha256 = ? WHERE id = ?", (blob.sha256, media_id))
        counts.adopted += 1
        if blob.new:
            counts.new_blobs += 1
            counts.new_bytes += blob.size
    return counts


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

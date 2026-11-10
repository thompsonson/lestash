"""Turn a snapshot's KoboReader.sqlite into LeStash items.

Each title becomes a parent item (``source_id`` = the Kobo ContentID). Highlights,
notes, dog-ears and pen markups become its children, linked through
``metadata._parent_source_id``. A book's file and a markup's images are media pointing
at their blobs in the file store, one row per version the snapshots have seen (#217).

Columns are read by name. Optional ones read as empty when missing, so a firmware
update that adds or drops one doesn't break the import; the keys (ContentID,
BookmarkID, VolumeID) are required.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lestash.core.blobs import BlobStore
from lestash.models.item import ItemCreate, MediaCreate

from lestash_kobo.store import DB_PATH, FileEntry, KoboStore, Snapshot

logger = logging.getLogger(__name__)

SOURCE_TYPE = "kobo"
# Bump when the items change shape, so the next sync re-imports an unchanged snapshot
READER_VERSION = 2  # 2: book files and markups as stored, versioned media
DEVICE_ROOT = "file:///mnt/onboard/"
MARKUP_PLACEHOLDER = "[Handwritten markup]"
BOOKMARK_PLACEHOLDER = "[Bookmark]"
KINDS = {"highlight": "Highlight", "note": "Note", "dogear": "Bookmark", "markup": "Markup"}


def parse_date(value: str | None) -> datetime | None:
    """Kobo dates are ISO strings, with or without ``Z`` and milliseconds; all UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _db_time(when: datetime) -> str:
    """A time as SQLite's CURRENT_TIMESTAMP writes it: UTC, to the second."""
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def _book_name(volume_id: str) -> str:
    """A readable name for a book from its ContentID: the file name, or the ID."""
    path = device_path(volume_id or "")
    return Path(path).stem if path else (volume_id or "")


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def device_path(content_id: str) -> str | None:
    """``file:///mnt/onboard/a/b.pdf`` → ``a/b.pdf`` (the path inside a snapshot).

    ContentIDs hold the raw path (spaces and all), not a URL-encoded one.
    """
    if not content_id.startswith(DEVICE_ROOT):
        return None
    return content_id[len(DEVICE_ROOT) :]


def title_kind(row: dict[str, Any]) -> str:
    content_id = row["ContentID"]
    if content_id.endswith(".nebo"):
        return "notebook"
    if "/.kobo/google_drive/" in content_id:
        return "drive_pdf" if (row.get("MimeType") or "").endswith("pdf") else "drive_file"
    if content_id.startswith(DEVICE_ROOT):
        return "sideloaded"
    return "store"


def open_db(path: Path) -> sqlite3.Connection:
    # immutable=1: the snapshot copy is read-only and nothing else writes to it
    conn = sqlite3.connect(f"file:{quote(str(path))}?immutable=1", uri=True)
    return _prepare(conn)


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    # Some TEXT columns hold binary (e.g. Bookmark.ExtraAnnotationData), which the
    # default factory refuses to decode
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


class KoboReader:
    def __init__(
        self,
        store: KoboStore,
        snapshot: Snapshot,
        include_store_titles: bool = False,
        files: BlobStore | None = None,
    ) -> None:
        """``files`` is the file store media are served from; a Kobo store keeping its own
        blobs (a ``store_dir`` of its own) has each attached file copied there."""
        self.store, self.snapshot = store, snapshot
        self.include_store_titles = include_store_titles
        self.files = files
        self._history: list[Snapshot] | None = None

    def _read_db(self) -> tuple[list[dict], list[dict], dict[str, dict]]:
        db = self.snapshot.blob(self.store, DB_PATH)
        if db is None:
            raise FileNotFoundError(f"snapshot {self.snapshot.name} has no {DB_PATH}")
        wal = self.snapshot.blob(self.store, DB_PATH + "-wal")
        with tempfile.TemporaryDirectory(prefix="lestash-kobo-") as tmp:
            if wal is not None and wal.stat().st_size > 0:
                # Changes not yet checkpointed live in the -wal file. SQLite only reads
                # it next to its database under the same name, so open copies of both.
                local = Path(tmp) / "KoboReader.sqlite"
                shutil.copyfile(db, local)
                shutil.copyfile(wal, Path(tmp) / "KoboReader.sqlite-wal")
                conn = _prepare(sqlite3.connect(local))
            else:
                conn = open_db(db)
            try:
                titles = _rows(conn, "SELECT * FROM content WHERE ContentType = '6'")
                marks = _rows(conn, "SELECT * FROM Bookmark")
                # Only the chapters bookmarks point at, and only the columns used
                chapters = {
                    r["ContentID"]: r
                    for r in _rows(
                        conn,
                        # SELECT *: optional columns may be missing on other firmware
                        "SELECT * FROM content WHERE ContentType = '9'"
                        " AND ContentID IN (SELECT ContentID FROM Bookmark)",
                    )
                }
            finally:
                conn.close()
        return titles, marks, chapters

    def items(self) -> Iterator[ItemCreate]:
        titles, marks, chapters = self._read_db()

        marks = [m for m in marks if str(m.get("Hidden")).lower() != "true"]
        marked = {m["VolumeID"] for m in marks}
        kept: set[str] = set()
        for row in titles:
            if self._keep(row, marked):
                kept.add(row["ContentID"])
                yield self.title_item(row)
        # Highlights can outlive their book on the device. Keep them: they still name
        # their book, so they attach to its item if an earlier import stored it, and
        # stay top-level otherwise. (A stand-in book item would overwrite that one.)
        known = {row["ContentID"] for row in titles}
        kept |= {
            v
            for v in marked
            if v and v not in known and not v.endswith(".nebo")  # #202
        }
        for mark in sorted(marks, key=lambda m: self._order(m, chapters)):
            if mark["VolumeID"] in kept:
                yield self.bookmark_item(mark, chapters)

    def _keep(self, row: dict[str, Any], marked: set[str]) -> bool:
        kind = title_kind(row)
        if kind == "notebook":  # notebooks are their own waypoint (#202)
            return False
        if kind != "store" or self.include_store_titles:
            return True
        # A store title you never downloaded or opened is a shop listing, not reading
        return (
            str(row.get("IsDownloaded")).lower() == "true"
            or row["ContentID"] in marked
            or _num(row.get("___PercentRead")) > 0
        )

    def title_item(self, row: dict[str, Any]) -> ItemCreate:
        kind = title_kind(row)
        title = (row.get("Title") or "").strip() or row["ContentID"]
        author = (row.get("Attribution") or "").strip() or None
        description = _text(row.get("Description"))
        content = description or " — ".join(p for p in (title, author) if p)
        # No reading progress here (percent read, last read): it changes with every
        # reading session, and each change would add an item-history row. Progress
        # is recorded as reading activity instead (#201).
        meta: dict[str, Any] = {
            "kind": kind,
            "content_id": row["ContentID"],
            "mime_type": row.get("MimeType"),
            "downloaded": str(row.get("IsDownloaded")).lower() == "true",
            "publisher": (row.get("Publisher") or "").strip("​ ") or None,
            "isbn": row.get("ISBN") or None,
            "series": row.get("Series") or None,
        }
        path = device_path(row["ContentID"])
        if path:
            meta["device_path"] = path
            entry = self.snapshot.files.get(path)
            if entry:
                meta["file_sha256"] = entry.sha256
        media = self._book_media(path, row.get("MimeType")) if path else None
        return ItemCreate(
            source_type=SOURCE_TYPE,
            source_id=row["ContentID"],
            title=title,
            content=content,
            author=author,
            created_at=parse_date(row.get("DateAdded")) or parse_date(row.get("___SyncTime")),
            metadata={k: v for k, v in meta.items() if v not in (None, "")},
            media=media,
        )

    def bookmark_item(self, mark: dict[str, Any], chapters: dict[str, dict]) -> ItemCreate:
        kind = (mark.get("Type") or "highlight").lower()
        text = (mark.get("Text") or "").strip()
        note = (mark.get("Annotation") or "").strip()
        placeholder = MARKUP_PLACEHOLDER if kind == "markup" else BOOKMARK_PLACEHOLDER
        content = "\n\n".join(p for p in (text, note) if p) or placeholder
        chapter = chapters.get(mark.get("ContentID") or "", {})
        chapter_title = _text(chapter.get("Title"))
        if re.search(r"\.x?html?$", chapter_title, re.I):
            chapter_title = ""  # a spine file name, not a chapter title
        label = KINDS.get(kind, kind.title())
        book = _book_name(mark["VolumeID"])
        snippet = " ".join((text or note).split())[:60]  # one line, for listings
        title = f"{label}: {snippet}" if snippet else label
        meta: dict[str, Any] = {
            "_parent_source_id": mark["VolumeID"],
            # Names the book even when the book item is gone (the child is then top-level)
            "book": book,
            "kind": kind,
            "chapter": chapter_title or None,
            "chapter_content_id": mark.get("ContentID"),
            "chapter_progress": mark.get("ChapterProgress"),
            "annotation": note or None,
            "start_path": mark.get("StartContainerPath"),
            "modified": mark.get("DateModified"),
        }
        return ItemCreate(
            source_type=SOURCE_TYPE,
            source_id=f"bookmark:{mark['BookmarkID']}",
            title=title,
            content=content,
            created_at=parse_date(mark.get("DateCreated")),
            metadata={k: v for k, v in meta.items() if v not in (None, "")},
            media=self._markup_media(mark["BookmarkID"]) if kind == "markup" else None,
        )

    def _versions(self, device_path: str) -> list[tuple[FileEntry, str, str | None]]:
        """Each version of a device file the snapshots up to this one have seen, oldest
        first: when it was first seen, and when the next one replaced it (None for the
        newest).

        Only while the file is in this snapshot: a file gone from the Kobo keeps the
        media rows an earlier import wrote.
        """
        if device_path not in self.snapshot.files:
            return []
        if self._history is None:
            self._history = [
                s for s in self.store.snapshots() if s.taken_at <= self.snapshot.taken_at
            ]
            if not any(s.name == self.snapshot.name for s in self._history):
                self._history.append(self.snapshot)
        seen: list[tuple[FileEntry, datetime]] = []
        for snap in self._history:
            entry = snap.files.get(device_path)
            if entry and (not seen or seen[-1][0].sha256 != entry.sha256):
                seen.append((entry, snap.taken_at))
        return [
            (entry, _db_time(when), _db_time(seen[i + 1][1]) if i + 1 < len(seen) else None)
            for i, (entry, when) in enumerate(seen)
        ]

    def _stored_media(
        self,
        device_path: str,
        media_type: str,
        mime_type: str | None,
        alt_text: str,
        position: int = 0,
    ) -> list[MediaCreate]:
        """A media row per version of the file, pointing at its blob; versions whose
        blob is lost are left out, so one lost file never blocks an import."""
        media = []
        for entry, seen_at, superseded_at in self._versions(device_path):
            blob = self.store.blob_path(entry.sha256)
            if not blob.is_file():
                logger.warning("Kobo file %s is missing from the store", device_path)
                continue
            if self.files is not None and self.files.path(entry.sha256) != blob:
                try:
                    self.files.put_file(blob)  # stored once; already there costs a lookup
                except OSError as e:
                    logger.warning("Could not store Kobo file %s: %s", device_path, e)
                    continue
            media.append(
                MediaCreate(
                    media_type=media_type,
                    mime_type=mime_type,
                    alt_text=alt_text,
                    position=position,
                    sha256=entry.sha256,
                    size=entry.size,
                    seen_at=seen_at,
                    superseded_at=superseded_at,
                )
            )
        return media

    def _book_media(self, path: str, mime_type: str | None) -> list[MediaCreate] | None:
        """The book's file: a PDF to open, or an EPUB to download. Kobo store books
        (no device path) are encrypted, and are left out."""
        mime = mimetypes.guess_type(path)[0] or mime_type
        media_type = "pdf" if mime == "application/pdf" else "file"
        return self._stored_media(path, media_type, mime, Path(path).name) or None

    def _markup_media(self, bookmark_id: str) -> list[MediaCreate] | None:
        """The pen strokes of a markup: a JPG of the page and an SVG of the ink. A
        redrawn markup adds a version at the same place."""
        media = []
        for pos, (ext, mime) in enumerate((("jpg", "image/jpeg"), ("svg", "image/svg+xml"))):
            media += self._stored_media(
                f".kobo/markups/{bookmark_id}.{ext}", "image", mime, "Kobo pen markup", pos
            )
        return media or None

    @staticmethod
    def _order(mark: dict[str, Any], chapters: dict[str, dict]) -> tuple:
        chapter = chapters.get(mark.get("ContentID") or "", {})
        return (
            str(mark.get("VolumeID") or ""),
            _num(chapter.get("VolumeIndex")),
            _float(mark.get("ChapterProgress")),
            str(mark.get("DateCreated") or ""),
        )

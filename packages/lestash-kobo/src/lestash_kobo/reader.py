"""Turn a snapshot's KoboReader.sqlite into LeStash items.

Each title becomes a parent item (``source_id`` = the Kobo ContentID). Highlights,
notes, dog-ears and pen markups become its children, linked through
``metadata._parent_source_id``. Markup images are copied into LeStash's media folder.

Columns are read by name, and missing ones read as empty, so a firmware update that
adds or drops a column doesn't break the import.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from lestash.models.item import ItemCreate, MediaCreate

from lestash_kobo.store import DB_PATH, KoboStore, Snapshot

SOURCE_TYPE = "kobo"
# Bump when the items change shape, so the next sync re-imports an unchanged snapshot
READER_VERSION = 1
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


def _text(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    """``file:///mnt/onboard/a/b.pdf`` → ``a/b.pdf`` (the path inside a snapshot)."""
    if not content_id.startswith(DEVICE_ROOT):
        return None
    return unquote(content_id[len(DEVICE_ROOT) :])


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
        media_dir: Path | None = None,
        include_store_titles: bool = False,
    ) -> None:
        self.store, self.snapshot = store, snapshot
        self.media_dir = media_dir
        self.include_store_titles = include_store_titles

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
                chapters = {
                    r["ContentID"]: r
                    for r in _rows(conn, "SELECT * FROM content WHERE ContentType = '9'")
                }
            finally:
                conn.close()
        return titles, marks, chapters

    def items(self) -> Iterator[ItemCreate]:
        self._sweep_media_temp()
        titles, marks, chapters = self._read_db()

        marks = [m for m in marks if str(m.get("Hidden")).lower() != "true"]
        marked = {m["VolumeID"] for m in marks}
        kept: set[str] = set()
        for row in titles:
            if self._keep(row, marked):
                kept.add(row["ContentID"])
                yield self.title_item(row)
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
        meta: dict[str, Any] = {
            "kind": kind,
            "content_id": row["ContentID"],
            "mime_type": row.get("MimeType"),
            "percent_read": _num(row.get("___PercentRead")),
            "read_status": _num(row.get("ReadStatus")),
            "time_spent_reading": _num(row.get("TimeSpentReading")),
            "last_read": row.get("DateLastRead") or None,
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
        return ItemCreate(
            source_type=SOURCE_TYPE,
            source_id=row["ContentID"],
            title=title,
            content=content,
            author=author,
            created_at=parse_date(row.get("DateAdded")) or parse_date(row.get("___SyncTime")),
            metadata={k: v for k, v in meta.items() if v not in (None, "")},
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
        snippet = (text or note)[:60]
        title = f"{label}: {snippet}" if snippet else label
        meta: dict[str, Any] = {
            "_parent_source_id": mark["VolumeID"],
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

    def _sweep_media_temp(self) -> None:
        """Remove markup temp files left by a killed sync. Imports hold the store lock."""
        if self.media_dir is not None:
            for leftover in (self.media_dir / "kobo").glob(".tmp-*"):
                leftover.unlink(missing_ok=True)

    def _markup_media(self, bookmark_id: str) -> list[MediaCreate] | None:
        """The pen strokes of a markup: a JPG of the page and an SVG of the ink.

        The file name follows the bookmark, not the contents, so a redrawn markup
        replaces its image instead of adding a second one.
        """
        if self.media_dir is None:
            return None
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", bookmark_id)
        media = []
        for pos, (ext, mime) in enumerate((("jpg", "image/jpeg"), ("svg", "image/svg+xml"))):
            entry = self.snapshot.files.get(f".kobo/markups/{bookmark_id}.{ext}")
            if not entry:
                continue
            rel = f"kobo/markup-{safe_id}.{ext}"
            dest = self.media_dir / rel
            if not dest.exists() or dest.stat().st_size != entry.size or _sha(dest) != entry.sha256:
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{dest.name}-", dir=dest.parent)
                os.close(fd)
                try:
                    shutil.copyfile(self.store.blob_path(entry.sha256), tmp_name)
                    os.replace(tmp_name, dest)
                except BaseException:
                    Path(tmp_name).unlink(missing_ok=True)
                    raise
            media.append(
                MediaCreate(
                    media_type="image",
                    local_path=rel,
                    mime_type=mime,
                    alt_text="Kobo pen markup",
                    position=pos,
                )
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

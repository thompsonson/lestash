"""Notes drawn on PDFs on the Kobo, read from each annotated state (#338, #200).

The Elipsa saves pen notes into the PDF file itself. Each snapshot where a book's bytes
changed is a new annotated state, kept as a version of the book's ``pdf`` media, with
the time it was first seen. A book's states are read in that order:

- Each annotation becomes a ``pdf_annotation`` child of the book, matched across states
  by its strokes and ``/NM``, so a later state adds only new ones. One erased is kept,
  marked ``removed_from_pdf``.
- Only states newer than the newest one read are applied: an older state applied late
  would mark newer notes erased. Bytes that come back later are a new state. After a
  newer extractor, the newest state read is read again first, then any newer ones, so
  earlier states don't erase and restore every note.
- A state whose file is gone from the store is passed over and reported once.
- A state with new notes records an ``annotated`` activity at the time it was first
  seen, naming the notes. The version the notes were drawn on is the book's
  ``alternate_of`` item, when it has one.
- Each state is recorded in ``blob_text``, per book (tool ``pdf-annotations:item-N``),
  by extractor version, with when it was seen and the notes first read from it. The
  notes themselves are not rewritten to say so.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lestash.core.activities import record_activities
from lestash.core.blobs import media_file
from lestash.core.config import Config
from lestash.core.pdf_enrich.persistence import ANNOTATION_SOURCE_TYPE, _upsert_annotations
from lestash.core.pdf_enrich.types import ExtractedAnnotation
from lestash.core.pdf_enrich.version import EXTRACTOR_VERSION
from lestash.models.item import ActivityCreate

from lestash_kobo.reader import SOURCE_TYPE

logger = logging.getLogger(__name__)

TOOL = "pdf-annotations"
VERB = "annotated"


@dataclass
class NotesResult:
    states: int = 0  # annotated states read
    added: int = 0  # notes new in them
    books: set[int] = field(default_factory=set)  # books with new notes
    errors: list[str] = field(default_factory=list)


@dataclass
class _State:
    sha256: str
    local_path: str | None
    seen: datetime  # when it was first seen


def tool(book_id: int) -> str:
    """The blob_text tool name of one book's states: the same bytes on two books are
    two states."""
    return f"{TOOL}:item-{book_id}"


def _when(value: object) -> datetime:
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _books(conn: sqlite3.Connection) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT m.item_id FROM item_media m JOIN items i ON i.id = m.item_id"
            " WHERE i.source_type = ? AND i.source_id NOT LIKE 'bookmark:%'"
            " AND m.media_type = 'pdf' AND m.sha256 IS NOT NULL ORDER BY m.item_id",
            (SOURCE_TYPE,),
        )
    ]


def _states(conn: sqlite3.Connection, book_id: int) -> list[_State]:
    """The book's PDF states, oldest first. A version keeps when it was seen in
    created_at; bytes that come back later are a new state, at their new time."""
    rows = conn.execute(
        "SELECT sha256, local_path, created_at FROM item_media"
        " WHERE item_id = ? AND media_type = 'pdf' AND sha256 IS NOT NULL",
        (book_id,),
    ).fetchall()
    return sorted((_State(r[0], r[1], _when(r[2])) for r in rows), key=lambda s: (s.seen, s.sha256))


def _read_up_to(conn: sqlite3.Connection, book_id: int) -> tuple[datetime | None, bool]:
    """The newest state time read for the book, and whether this extractor read it."""
    newest: tuple[datetime, bool] | None = None
    for version, text in conn.execute(
        "SELECT tool_version, text FROM blob_text WHERE tool = ?", (tool(book_id),)
    ):
        seen = json.loads(text).get("seen")
        if not seen:
            continue
        mark = (_when(seen), version == str(EXTRACTOR_VERSION))
        if newest is None or mark[0] > newest[0] or (mark[0] == newest[0] and mark[1]):
            newest = mark
    return (newest[0], newest[1]) if newest else (None, False)


def _to_read(conn: sqlite3.Connection, book_id: int) -> list[_State]:
    """The states to apply, oldest first: those newer than the newest read. After a
    newer extractor, the newest state the older one read is read again first, so the
    notes are matched once rather than erased and restored state by state."""
    states = _states(conn, book_id)
    last, current = _read_up_to(conn, book_id)
    if last is None:
        return states
    newer = [s for s in states if s.seen > last]
    if current:
        return newer
    again = [s for s in states if s.seen <= last][-1:]
    return again + newer


def _children(conn: sqlite3.Connection, book_id: int) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE parent_id = ? AND source_type = ?",
            (book_id, ANNOTATION_SOURCE_TYPE),
        )
    }


def _record(
    conn: sqlite3.Connection,
    book_id: int,
    state: _State,
    found: int | None,
    new: list[int],
    missing: bool = False,
) -> None:
    """Record a state read: when it was seen (the book's place in its states), what
    was in it, and the notes first read from it."""
    # The same bytes on this book before (a file put back): keep the notes first read then
    before = conn.execute(
        "SELECT text FROM blob_text WHERE sha256 = ? AND tool = ?", (state.sha256, tool(book_id))
    ).fetchone()
    earlier = json.loads(before[0]).get("new_notes", []) if before else []
    notes = sorted(set(earlier) | set(new))
    record = {"seen": state.seen.isoformat(), "annotations": found, "new_notes": notes}
    if missing:
        record["missing"] = True
    conn.execute(
        "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text) VALUES (?, ?, ?, ?)",
        (state.sha256, tool(book_id), str(EXTRACTOR_VERSION), json.dumps(record)),
    )


def _read(path: Path) -> list[ExtractedAnnotation] | None:
    """The annotations in a PDF, or None if PyMuPDF can't read it as a PDF.

    A file that can't be opened raises OSError, so the state is tried again.
    """
    import pymupdf
    from lestash.core.pdf_enrich.annotations import extract_annotations

    try:
        doc = pymupdf.open(path)
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as e:
        logger.warning("%s is not a PDF PyMuPDF can read: %s", path, e)
        return None
    with doc:
        return extract_annotations(doc)


def extract_notes(conn: sqlite3.Connection, config: Config) -> NotesResult:
    """Read every annotated state not read yet. Commits after each state."""
    result = NotesResult()
    for book_id in _books(conn):
        for state in _to_read(conn, book_id):
            path = media_file({"sha256": state.sha256, "local_path": state.local_path}, config)
            if path is None:
                # Gone from the store: passed over, so the book's later states still come
                _record(conn, book_id, state, None, [], missing=True)
                conn.commit()
                result.errors.append(
                    f"book {book_id}: file {state.sha256[:12]} is not in the store; passed over"
                )
                continue
            try:
                annotations = _read(path)
                before = _children(conn, book_id)
                if annotations is not None:
                    _upsert_annotations(conn, book_id, annotations)
                new = sorted(_children(conn, book_id) - before)  # never seen before
                if new:
                    record_activities(
                        conn,
                        book_id,
                        [
                            ActivityCreate(
                                verb=VERB,
                                occurred_at=state.seen,
                                source=SOURCE_TYPE,
                                details={"sha256": state.sha256, "notes": new},
                            )
                        ],
                    )
                    result.books.add(book_id)
                    result.added += len(new)
                found = None if annotations is None else len(annotations)
                _record(conn, book_id, state, found, new)
                conn.commit()
            except Exception as e:
                # Tried again at the next run; later states wait, so they never come first
                conn.rollback()
                logger.warning("Reading notes from book %d failed: %s", book_id, e)
                result.errors.append(f"book {book_id}: {e}")
                break
            result.states += 1
    return result

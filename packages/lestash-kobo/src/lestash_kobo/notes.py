"""Notes drawn on PDFs on the Kobo, read from each annotated state (#338, #200).

The Elipsa saves pen notes into the PDF file itself. Each snapshot where a book's bytes
changed is a new annotated state, kept as a version of the book's ``pdf`` media, with
the time it was first seen. A book's states are read in that order:

- Each annotation becomes a ``pdf_annotation`` child of the book, matched across states
  by its strokes and ``/NM``, so a later state adds only new ones. One erased is kept,
  marked ``removed_from_pdf``.
- Only states newer than the last one read are applied: an older state applied late
  would mark newer notes erased. After a newer extractor, only the newest state is read
  again, so earlier states don't erase and restore every note.
- A state with new notes records an ``annotated`` activity at the time it was first
  seen, naming the notes. The version the notes were drawn on is the book's
  ``alternate_of`` item, when it has one.
- Each state is recorded in ``blob_text``, per book (tool ``pdf-annotations:item-N``),
  by extractor version, with the notes first read from it. The notes themselves are
  not rewritten to say so.
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
    read: str | None  # the extractor version that read it, if one did


def tool(book_id: int) -> str:
    """The blob_text tool name of one book's states: the same bytes on two books are
    two states."""
    return f"{TOOL}:item-{book_id}"


def _when(value: object) -> datetime:
    when = datetime.fromisoformat(str(value).replace(" ", "T"))
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
    """The book's PDF states, oldest first: a version keeps when it was seen in
    created_at. The same bytes seen twice are one state, at their first time."""
    states: dict[str, _State] = {}
    for sha, local_path, created, read in conn.execute(
        "SELECT m.sha256, m.local_path, m.created_at, t.tool_version FROM item_media m"
        " LEFT JOIN blob_text t ON t.sha256 = m.sha256 AND t.tool = ?"
        " WHERE m.item_id = ? AND m.media_type = 'pdf' AND m.sha256 IS NOT NULL",
        (tool(book_id), book_id),
    ):
        seen = _when(created)
        if sha not in states or seen < states[sha].seen:
            states[sha] = _State(sha, local_path, seen, read)
    return sorted(states.values(), key=lambda s: (s.seen, s.sha256))


def _to_read(states: list[_State]) -> tuple[list[_State], list[_State]]:
    """The states to apply, oldest first, and the ones to record without applying."""
    current = str(EXTRACTOR_VERSION)
    done = [s for s in states if s.read == current]
    unread = [s for s in states if s.read != current]
    if not unread:
        return [], []
    if done:
        last = done[-1].seen
        return [s for s in unread if s.seen > last], [s for s in unread if s.seen <= last]
    if any(s.read is not None for s in states):
        # Read before by an older extractor: read the newest state again, not them all
        return unread[-1:], unread[:-1]
    return unread, []


def _children(conn: sqlite3.Connection, book_id: int) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE parent_id = ? AND source_type = ?",
            (book_id, ANNOTATION_SOURCE_TYPE),
        )
    }


def _record(
    conn: sqlite3.Connection, book_id: int, state: _State, found: int | None, new: list[int]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text) VALUES (?, ?, ?, ?)",
        (
            state.sha256,
            tool(book_id),
            str(EXTRACTOR_VERSION),
            json.dumps({"annotations": found, "new_notes": new}),
        ),
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
        apply, skip = _to_read(_states(conn, book_id))
        for state in skip:
            _record(conn, book_id, state, None, [])
        conn.commit()
        for state in apply:
            path = media_file({"sha256": state.sha256, "local_path": state.local_path}, config)
            try:
                if path is None:
                    raise OSError(f"file {state.sha256[:12]} is not in the store")
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
                # Later states wait, so they are never applied before this one
                conn.rollback()
                logger.warning("Reading notes from book %d failed: %s", book_id, e)
                result.errors.append(f"book {book_id}: {e}")
                break
            result.states += 1
    return result

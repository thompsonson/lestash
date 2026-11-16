"""Notes drawn on PDFs on the Kobo, read from each annotated state (#338, #200).

The Elipsa saves pen notes into the PDF file itself. Each snapshot where a book's bytes
changed is a new annotated state, kept as a version of the book's ``pdf`` media. Each
state not read yet is read once, oldest first, with the enricher's ink reader:

- Each annotation becomes a ``pdf_annotation`` child of the book, matched across states
  by its strokes and ``/NM``, so a later state adds only new ones. One erased is kept,
  marked ``removed_from_pdf``.
- A child records the state it was first read from (``first_seen_in``, its SHA-256).
  The version it was drawn on is the book's ``alternate_of`` item, when it has one.
- A state with new notes records an ``annotated`` activity at the time the state was
  first seen.
- A state read is recorded in ``blob_text`` (tool ``pdf-annotations``), by extractor
  version: a newer extractor reads every state again.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lestash.core.activities import record_activities
from lestash.core.blobs import media_file
from lestash.core.config import Config
from lestash.core.pdf_enrich.persistence import ANNOTATION_SOURCE_TYPE, _upsert_annotations
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


def _unread(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Each Kobo book's PDF states not read by this extractor, oldest first per book."""
    return conn.execute(
        """
        SELECT m.item_id, m.sha256, m.local_path, m.version, m.created_at
        FROM item_media m
        JOIN items i ON i.id = m.item_id
        WHERE i.source_type = ? AND i.source_id NOT LIKE 'bookmark:%'
          AND m.media_type = 'pdf' AND m.sha256 IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM blob_text t
              WHERE t.sha256 = m.sha256 AND t.tool = ? AND t.tool_version = ?
          )
        ORDER BY m.item_id, m.version, m.id
        """,
        (SOURCE_TYPE, TOOL, str(EXTRACTOR_VERSION)),
    ).fetchall()


def _live_children(conn: sqlite3.Connection, book_id: int) -> set[int]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE parent_id = ? AND source_type = ?"
            " AND json_extract(metadata, '$.removed_from_pdf') IS NULL",
            (book_id, ANNOTATION_SOURCE_TYPE),
        )
    }


def _first_seen(conn: sqlite3.Connection, book_id: int) -> dict[int, str | None]:
    """Each note of the book, with the state it was first read from."""
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT id, json_extract(metadata, '$.first_seen_in') FROM items"
            " WHERE parent_id = ? AND source_type = ?",
            (book_id, ANNOTATION_SOURCE_TYPE),
        )
    }


def _state_time(row: sqlite3.Row) -> datetime:
    """When the state was first seen: a media version keeps it in created_at."""
    when = datetime.fromisoformat(str(row["created_at"]).replace(" ", "T"))
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def extract_notes(conn: sqlite3.Connection, config: Config) -> NotesResult:
    """Read every annotated state not read yet. Commits after each state."""
    import pymupdf
    from lestash.core.pdf_enrich.annotations import extract_annotations

    result = NotesResult()
    for row in _unread(conn):
        book_id, sha = row["item_id"], row["sha256"]
        path = media_file({"sha256": sha, "local_path": row["local_path"]}, config)
        if path is None:
            result.errors.append(f"book {book_id}: file {sha[:12]} is not in the store")
            continue
        try:
            with pymupdf.open(path) as doc:
                annotations = extract_annotations(doc)
        except Exception as e:  # a PDF PyMuPDF can't read: recorded, not retried
            logger.warning("Reading notes from book %d (%s) failed: %s", book_id, sha[:12], e)
            annotations = None
        before = _live_children(conn, book_id)
        try:
            if annotations is not None:
                seen_in = _first_seen(conn, book_id)
                _upsert_annotations(conn, book_id, annotations)
                # The enricher rewrites a note's metadata when it refreshes it: put each
                # note's first state back, and give new notes this one
                for child_id, now in _first_seen(conn, book_id).items():
                    first = seen_in.get(child_id) or sha
                    if now != first:  # write only a change, so history stays quiet
                        conn.execute(
                            "UPDATE items SET metadata ="
                            " json_set(metadata, '$.first_seen_in', ?) WHERE id = ?",
                            (first, child_id),
                        )
            new = len(_live_children(conn, book_id) - before)
            if new:
                record_activities(
                    conn,
                    book_id,
                    [
                        ActivityCreate(
                            verb=VERB,
                            occurred_at=_state_time(row),
                            source=SOURCE_TYPE,
                            details={"sha256": sha, "notes": new},
                        )
                    ],
                )
                result.books.add(book_id)
                result.added += new
            conn.execute(
                "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text)"
                " VALUES (?, ?, ?, ?)",
                (
                    sha,
                    TOOL,
                    str(EXTRACTOR_VERSION),
                    json.dumps({"annotations": None if annotations is None else len(annotations)}),
                ),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.exception("Storing notes from book %d failed", book_id)
            result.errors.append(f"book {book_id}: {e}")
            continue
        result.states += 1
    return result

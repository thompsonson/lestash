"""Text and Markdown files you add to LeStash, kept as their text.

A .txt or .md file you add is an item of source type ``file``, keyed by its bytes like a
PDF: the same file is never added twice. Its content is the file's text; its kind is
"document".
"""

from __future__ import annotations

import hashlib
import sqlite3

from lestash.core.pdfs import Added

SOURCE_TYPE = "file"
FORMATS = {".txt": "text", ".md": "markdown", ".markdown": "markdown"}


def text_format(filename: str) -> str | None:
    """ "text" or "markdown" for a file name this module takes, else None."""
    name = filename.lower()
    return next((fmt for suffix, fmt in FORMATS.items() if name.endswith(suffix)), None)


def source_id_of(sha256: str) -> str:
    return sha256[:16]


def add_text_file(conn: sqlite3.Connection, data: bytes, filename: str) -> Added:
    """Add a text or Markdown file as an item. A file already held is not added again.

    The caller commits.
    """
    from lestash.core.database import upsert_item
    from lestash.models.item import ItemCreate

    fmt = text_format(filename)
    if fmt is None:
        raise ValueError(f"Not a text or Markdown file: {filename}")
    sha = hashlib.sha256(data).hexdigest()
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (SOURCE_TYPE, source_id_of(sha)),
    ).fetchone()
    if row:
        return Added(row[0], False, sha)

    name = filename.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] or name
    # utf-8-sig drops a byte-order mark; a stray byte is replaced, not refused
    text = data.decode("utf-8-sig", errors="replace")
    item_id = upsert_item(
        conn,
        ItemCreate(
            source_type=SOURCE_TYPE,
            source_id=source_id_of(sha),
            title=stem,
            content=text or f"[Empty file: {name}]",
            is_own_content=True,
            metadata={"filename": name, "format": fmt, "size_bytes": len(data), "sha256": sha},
        ),
    )
    return Added(item_id, True, sha)

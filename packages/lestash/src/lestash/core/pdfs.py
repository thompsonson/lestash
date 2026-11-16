"""PDFs you add to LeStash, and their versions (#338).

A PDF you add is an item of source type ``pdf``, keyed by its bytes: the same file is
never added twice. Each version of a document is its own item. A newer version links to
the one before it with ``revision_of`` (``prov:wasRevisionOf``), so the notes made on
each version stay with that version.

Versions form one line: a version has at most one version before it and one after it.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lestash.core.config import Config

SOURCE_TYPE = "pdf"
REVISION = "revision_of"
# Who made a revision link: you, by hand or through the app
REVISION_ORIGIN = "manual"
ALTERNATE = "alternate_of"
# Who links a copy to its PDF: the same bytes
COPY_ORIGIN = "linker:sha256"


class VersionError(ValueError):
    """A revision link that would break the line of versions."""


@dataclass(frozen=True)
class Added:
    item_id: int
    new: bool
    sha256: str


def source_id_of(sha256: str) -> str:
    """The item key of a PDF with these bytes; uploads have always used this."""
    return sha256[:16]


def find_pdf(conn: sqlite3.Connection, sha256: str) -> int | None:
    """The PDF item holding exactly these bytes, if there is one."""
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
        (SOURCE_TYPE, source_id_of(sha256)),
    ).fetchone()
    return row[0] if row else None


def _title(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".pdf") else name


def add_pdf(
    conn: sqlite3.Connection, config: Config, data: bytes, filename: str, *, extract: bool = True
) -> Added:
    """Add a PDF as an item, with its file. A file already held is not added again.

    ``extract`` turns the PDF into text for search (slow on a long PDF); without it the
    item's content is a placeholder. The caller commits, then may run
    ``pdf_enrich.enrich_item`` on a new item.
    """
    from lestash.core.database import save_media_file, upsert_item
    from lestash.core.pdf_enrich.persistence import set_source_pdf
    from lestash.models.item import ItemCreate

    sha = hashlib.sha256(data).hexdigest()
    held = find_pdf(conn, sha)
    if held is not None:
        return Added(held, False, sha)

    name = filename.rsplit("/", 1)[-1]
    markdown = None
    if extract:
        from lestash.core.text_extract import extract_content

        with tempfile.TemporaryDirectory(prefix="lestash-pdf-") as tmp:
            path = Path(tmp) / "file.pdf"
            path.write_bytes(data)
            markdown = extract_content(path, "application/pdf")

    item_id = upsert_item(
        conn,
        ItemCreate(
            source_type=SOURCE_TYPE,
            source_id=source_id_of(sha),
            title=_title(name),
            content=markdown or f"[PDF: {name}]",
            is_own_content=True,
            metadata={"filename": name, "size_bytes": len(data), "sha256": sha},
        ),
    )
    rel_path = save_media_file(item_id, data, name, config)
    set_source_pdf(conn, config, item_id, local_path=rel_path, source_origin="upload")
    link_copies(conn)  # a copy already held, such as one sideloaded to the Kobo
    return Added(item_id, True, sha)


def _revision_ends(conn: sqlite3.Connection, item_id: int) -> tuple[int | None, int | None]:
    """The confirmed version before this item, and the one after it."""
    before = conn.execute(
        "SELECT to_id FROM item_links WHERE from_id = ? AND kind = ? AND status = 'confirmed'",
        (item_id, REVISION),
    ).fetchone()
    after = conn.execute(
        "SELECT from_id FROM item_links WHERE to_id = ? AND kind = ? AND status = 'confirmed'",
        (item_id, REVISION),
    ).fetchone()
    return (before[0] if before else None, after[0] if after else None)


def versions(conn: sqlite3.Connection, item_id: int) -> list[int]:
    """Every version of the item's document, newest first, the item included."""
    newest = item_id
    seen = {item_id}
    while (after := _revision_ends(conn, newest)[1]) is not None and after not in seen:
        seen.add(after)
        newest = after
    line = [newest]
    while (before := _revision_ends(conn, line[-1])[0]) is not None and before not in line:
        line.append(before)
    return line


def older_problem(conn: sqlite3.Connection, older_id: int) -> str | None:
    """Why ``older_id`` can't take a next version, or None if it can."""
    if not conn.execute("SELECT 1 FROM items WHERE id = ?", (older_id,)).fetchone():
        return f"item {older_id} doesn't exist"
    _, after = _revision_ends(conn, older_id)
    if after is not None:
        return f"item {older_id} already has a next version, item {after}"
    return None


def link_revision(conn: sqlite3.Connection, newer_id: int, older_id: int) -> bool:
    """Record that ``newer_id`` is the next version of ``older_id``. False if it already was.

    Raises VersionError if either item is missing, or the link would give a version two
    versions before or after it, or make a loop. The caller commits.
    """
    if newer_id == older_id:
        raise VersionError("an item can't be a version of itself")
    for item_id in (newer_id, older_id):
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise VersionError(f"item {item_id} doesn't exist")
    before, _ = _revision_ends(conn, newer_id)
    _, after = _revision_ends(conn, older_id)
    if before == older_id:
        return False
    if before is not None:
        raise VersionError(f"item {newer_id} is already the next version of item {before}")
    if after is not None:
        raise VersionError(f"item {older_id} already has a next version, item {after}")
    if older_id in versions(conn, newer_id):
        raise VersionError(f"item {older_id} is already a later version of item {newer_id}")
    # An explicit link overrides one removed earlier
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
        "VALUES (?, ?, ?, ?, 'confirmed') "
        "ON CONFLICT (from_id, to_id, kind) DO UPDATE SET status = 'confirmed'",
        (newer_id, older_id, REVISION, REVISION_ORIGIN),
    )
    return True


def link_copies(conn: sqlite3.Connection) -> int:
    """Link each item holding a PDF's exact bytes to that PDF, ``alternate_of``.

    A Kobo book copied from a PDF item holds the copied bytes as its first file version,
    so it is linked even after notes drawn on it change the file. Rejected links stay
    rejected. Returns how many were added; the caller commits.
    """
    pdfs = conn.execute(
        "SELECT id, source_id, json_extract(metadata, '$.sha256') FROM items WHERE source_type = ?",
        (SOURCE_TYPE,),
    ).fetchall()
    links: set[tuple[int, int, str]] = set()
    for pdf_id, key, full in pdfs:
        if not key:
            continue
        # The item key is the hash's start: a range on the indexed hash finds its copies
        for copy_id, sha in conn.execute(
            "SELECT item_id, sha256 FROM item_media WHERE sha256 >= ? AND sha256 < ?",
            (key, key + "g"),
        ):
            if copy_id != pdf_id and (full is None or sha == full):
                links.add((copy_id, pdf_id, sha))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, status)"
        f" VALUES (?, ?, '{ALTERNATE}', ?, '{COPY_ORIGIN}', 'confirmed')",
        sorted(links),
    )
    return conn.total_changes - before

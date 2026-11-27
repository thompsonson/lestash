"""Put a converted document's pictures in place of its `<!-- image -->` markers (#394).

Docling turns each picture in a Word file into `<!-- image -->`. The PDF enricher
fills those markers for PDFs; this does it for Word files and Google Docs, from the
pictures `text_extract.convert_docx` returns.

Files are saved first, with no write open; the rows and the new content are then
written in one short transaction (#401). A rerun with the same pictures reuses the
same rows, and drops this module's rows for pictures the document no longer has.
"""

from __future__ import annotations

import hashlib
import logging

from lestash.core.config import Config
from lestash.core.database import (
    add_item_media,
    get_connection,
    mark_recent_history,
    max_history_id,
    save_media_file,
    write_transaction,
)
from lestash.core.pdf_enrich.images import apply_images, count_placeholders
from lestash.core.text_extract import DocPicture

logger = logging.getLogger(__name__)

PICTURE_ORIGIN = "document"

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def attach_pictures(
    item_id: int, pictures: list[DocPicture | None], *, config: Config | None = None
) -> int:
    """Save the item's pictures and link each one from its marker; returns how many.

    `pictures[i]` belongs to the i-th marker. If the item's content no longer has
    one marker per picture, it was not written by this conversion, so nothing changes.
    """
    if not any(pictures):
        return 0
    config = config or Config.load()
    files: dict[str, str] = {}
    for picture in pictures:
        if picture is None:
            continue
        digest = hashlib.sha256(picture.data).hexdigest()
        if digest not in files:
            name = f"{digest[:12]}{_EXTENSIONS.get(picture.mime_type, '.png')}"
            files[digest] = save_media_file(item_id, picture.data, name, config=config)

    with get_connection(config) as conn, write_transaction(conn):
        row = conn.execute("SELECT content FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return 0
        content = row[0] or ""
        if count_placeholders(content) != len(pictures):
            logger.warning(
                "Item %d has %d image markers, not %d; its pictures are left out",
                item_id,
                count_placeholders(content),
                len(pictures),
            )
            return 0

        replacements: dict[int, str] = {}
        kept: list[int] = []
        for index, picture in enumerate(pictures):
            if picture is None:
                continue
            alt_text = f"Picture {index + 1}"
            media_id = add_item_media(
                conn,
                item_id,
                media_type="image",
                local_path=files[hashlib.sha256(picture.data).hexdigest()],
                mime_type=picture.mime_type,
                alt_text=alt_text,
                position=index,
                source_origin=PICTURE_ORIGIN,
                _commit=False,
            )
            if media_id not in kept:
                # A reused row may carry an older place; only this module's own rows
                conn.execute(
                    "UPDATE item_media SET alt_text = ?, position = ? "
                    "WHERE id = ? AND source_origin = ?",
                    (alt_text, index, media_id, PICTURE_ORIGIN),
                )
                kept.append(media_id)
            replacements[index] = f"![{alt_text}](/api/media/{media_id})"

        conn.execute(
            "DELETE FROM item_media WHERE item_id = ? AND source_origin = ? AND media_type = ?"
            f" AND id NOT IN ({','.join('?' * len(kept))})",
            (item_id, PICTURE_ORIGIN, "image", *kept),
        )
        pre_max = max_history_id(conn)
        conn.execute(
            "UPDATE items SET content = ? WHERE id = ?",
            (apply_images(content, replacements), item_id),
        )
        mark_recent_history(conn, pre_max, "sync", item_ids=[item_id])
    return len(replacements)

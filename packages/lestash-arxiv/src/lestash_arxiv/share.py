"""A shared arXiv address becomes the paper's arXiv item, with its PDF (#395).

Sharing a paper saves it as its ``arxiv`` item: title, authors, abstract. Then its
PDF is stored as the item's source PDF and the PDF enricher reads it, so the item
holds the paper's text and pictures. A paper already held is reused, not added again.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from lestash.core.activities import record_saved
from lestash.core.config import Config
from lestash.core.database import upsert_item

from lestash_arxiv.client import Paper, get_paper
from lestash_arxiv.source import SOURCE_TYPE, paper_to_item

# New ids (1602.05629) and old ones (hep-th/9901001, math.GT/0309136), with a version
_ID = r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
_ADDRESS = re.compile(
    rf"^https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/{_ID}(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
PDF_LIMIT = 100 * 1024 * 1024  # bytes; a paper with many figures is a few tens of MB
TIMEOUT_S = 60
USER_AGENT = "LeStash (personal knowledge base)"


@dataclass(frozen=True)
class Imported:
    item_id: int
    title: str
    created: bool  # True if a new item was made, False if the paper was already held
    has_pdf: bool  # the item already has its source PDF


def arxiv_id_from_url(url: str) -> str | None:
    """The paper's arXiv id, without its version, from an arXiv address; else None."""
    match = _ADDRESS.match(url.strip())
    return match.group(1) if match else None


def find_paper(conn: sqlite3.Connection, arxiv_id: str) -> int | None:
    """The paper's arXiv item. Papers cited in a thesis were keyed ``arxiv:<id>``."""
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = ? AND source_id IN (?, ?) "
        "ORDER BY source_id = ? DESC, id LIMIT 1",
        (SOURCE_TYPE, arxiv_id, f"arxiv:{arxiv_id}", arxiv_id),
    ).fetchone()
    return row[0] if row else None


def has_source_pdf(conn: sqlite3.Connection, item_id: int) -> bool:
    from lestash.core.pdf_enrich.persistence import SOURCE_PDF_MEDIA_TYPE

    row = conn.execute(
        "SELECT 1 FROM item_media WHERE item_id = ? AND media_type = ? AND local_path IS NOT NULL",
        (item_id, SOURCE_PDF_MEDIA_TYPE),
    ).fetchone()
    return row is not None


def _metadata(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}


def _add_text(conn: sqlite3.Connection, item_id: int, key: str, text: str) -> None:
    """Keep text shared with a paper already held, after any it has under ``key``."""
    meta = _metadata(conn, item_id)
    held = meta.get(key)
    if held and text in held:
        return
    meta[key] = f"{held}\n\n{text}" if held else text
    conn.execute("UPDATE items SET metadata = ? WHERE id = ?", (json.dumps(meta), item_id))


def import_paper(
    conn: sqlite3.Connection,
    arxiv_id: str,
    *,
    note: str | None = None,
    shared: Mapping[str, str | None] | None = None,
    parent_id: int | None = None,
    fetch: Callable[[str], Paper | None] = get_paper,
) -> Imported | None:
    """Save the paper as its arXiv item, or reuse the one held. None if arXiv has no such
    paper. Records the share on the Timeline and commits; the PDF is ``attach_pdf``'s.

    ``shared`` is what came with the share, such as ``shared_text`` and
    ``shared_title``; each is kept in the item's metadata, as a link's would be.
    """
    kept = {key: text for key, text in (shared or {}).items() if text}
    held = find_paper(conn, arxiv_id)
    if held is not None:
        # Shared again: a new time on the Timeline; a held paper stays where it is
        for key, text in ({"notes": note} | kept).items():
            if text:
                _add_text(conn, held, key, text)
        record_saved(conn, held)
        conn.commit()
        row = conn.execute("SELECT title FROM items WHERE id = ?", (held,)).fetchone()
        title = (row[0] if row else None) or arxiv_id
        return Imported(held, title, created=False, has_pdf=has_source_pdf(conn, held))

    paper = fetch(arxiv_id)
    if paper is None:
        return None
    item = paper_to_item(paper)
    # The enricher replaces the text with the PDF's, so the abstract is kept here
    item.metadata = {**(item.metadata or {}), "abstract": paper.abstract, **kept}
    if note:
        item.metadata["notes"] = note
    item.parent_id = parent_id
    item_id = upsert_item(conn, item)
    record_saved(conn, item_id)
    conn.commit()
    return Imported(item_id, paper.title, created=True, has_pdf=False)


def pdf_url_of(conn: sqlite3.Connection, item_id: int) -> str | None:
    url = _metadata(conn, item_id).get("pdf_url")
    if url:
        return str(url)
    row = conn.execute(
        "SELECT source_id FROM items WHERE id = ? AND source_type = ?", (item_id, SOURCE_TYPE)
    ).fetchone()
    return f"https://arxiv.org/pdf/{row[0].removeprefix('arxiv:')}" if row else None


def download_pdf(url: str) -> bytes:
    """The PDF's bytes. Raises ValueError if the answer isn't a PDF or is too large."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
        data = response.read(PDF_LIMIT + 1)
    if len(data) > PDF_LIMIT:
        raise ValueError(f"the PDF is larger than {PDF_LIMIT // (1024 * 1024)} MB")
    if not data.startswith(b"%PDF"):
        raise ValueError("arXiv did not return a PDF")
    return data


def attach_pdf(
    item_id: int,
    *,
    config: Config,
    download: Callable[[str], bytes] = download_pdf,
) -> str:
    """Download the paper's PDF, store it as the item's source PDF and enrich the item.

    Returns the enricher's status, or ``"held"`` if the item already has its PDF.
    Raises on a failed download. Slow: run it after answering the request.
    """
    from lestash.core.database import get_connection
    from lestash.core.pdf_enrich import attach_source_pdf_and_enrich

    with get_connection(config) as conn:
        if has_source_pdf(conn, item_id):
            return "held"
        url = pdf_url_of(conn, item_id)
        row = conn.execute("SELECT source_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if url is None or row is None:
        raise ValueError(f"item {item_id} is not an arXiv paper")
    data = download(url)
    filename = f"{row[0].removeprefix('arxiv:').replace('/', '_')}.pdf"
    result = attach_source_pdf_and_enrich(item_id, data, filename, drive_url=url, config=config)
    return result.status

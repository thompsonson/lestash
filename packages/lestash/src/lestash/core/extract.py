"""Text from files that have none: NotebookLM slides, infographics and apps (#218, W3).

Text is read once per stored file and kept in ``blob_text`` (by hash and tool), so a
file in two items, or an item imported again, is never read twice. An item's text is
its own content, then a "## Text from the files" part built from its current media:
``## Slide 7`` and the slide's text for each slide, the text of an infographic, an
app's page text. The part is rebuilt from ``blob_text`` whenever the item is stored,
so an import keeps it and adds no history.

Slides and infographics are pictures: RapidOCR's English model reads them (the same
OCR Docling uses; Docling itself drops most of a slide's text as a picture). Apps are
web pages: their visible text is kept.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from lestash.core.blobs import BlobStore, store_root
from lestash.core.config import Config

logger = logging.getLogger(__name__)

PART = "## Text from the files"
OCR_TOOL = "rapidocr-en"
HTML_TOOL = "html-text"
# Which items get text: NotebookLM artifacts of these kinds
ARTIFACT_KINDS = frozenset({"slides", "infographic", "app"})


def wants_text(source_type: str, metadata: dict[str, Any] | None) -> bool:
    metadata = metadata or {}
    return (
        source_type == "notebooklm"
        and metadata.get("type") == "artifact"
        and metadata.get("artifact_type") in ARTIFACT_KINDS
    )


def tool_for(media_type: str, mime_type: str | None) -> str | None:
    """The tool that reads a media row's file, or None if none does."""
    if media_type == "image" and (mime_type or "").startswith("image/"):
        return OCR_TOOL
    if mime_type == "text/html":
        return HTML_TOOL
    return None


# --------------------------------------------------------------------------- #
# Reading files


class _Ocr:
    """RapidOCR, loaded once: its models take a second to load."""

    _engine: Any = None
    version = ""

    @classmethod
    def read(cls, path: Path) -> str:
        if cls._engine is None:
            import rapidocr
            from rapidocr import LangRec, OCRVersion, RapidOCR

            # The default model is Chinese, and runs English words together
            cls._engine = RapidOCR(
                params={"Rec.lang_type": LangRec.EN, "Rec.ocr_version": OCRVersion.PPOCRV5}
            )
            cls.version = f"rapidocr {rapidocr.__version__}, PP-OCRv5 en"
        result = cls._engine(str(path))
        return "\n".join(t.strip() for t in (result.txts or ()) if t.strip())


class _PageText(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(" ".join(data.split()))


def page_text(path: Path) -> str:
    """A web page's visible text, one run of text per line. Only parsed, never run."""
    parser = _PageText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return "\n".join(parser.parts)


def read_file(tool: str, path: Path) -> tuple[str, str]:
    """The text of a file and the tool's version."""
    if tool == OCR_TOOL:
        text = _Ocr.read(path)
        return text, _Ocr.version
    if tool == HTML_TOOL:
        return page_text(path), "html.parser"
    raise ValueError(f"unknown tool {tool}")


# --------------------------------------------------------------------------- #
# Building an item's text


@dataclass(frozen=True)
class Media:
    media_type: str
    mime_type: str | None
    position: int
    alt_text: str | None
    sha256: str


def base_content(content: str | None) -> str:
    """An item's own content, without the part built from its files."""
    content = content or ""
    cut = content.find("\n\n" + PART + "\n")
    if cut < 0 and content.startswith(PART + "\n"):
        cut = 0
    return content[:cut] if cut >= 0 else content


Texts = dict[tuple[str, str], tuple[str, str]]  # (sha256, tool): (text, tool version)


def compose(content: str | None, media: Iterable[Media], texts: Texts) -> tuple[str, list[str]]:
    """``content`` with its files' text, and the tool versions that read it."""
    sections = []
    versions: set[str] = set()
    seen: set[tuple[str, str, int]] = set()
    for m in sorted(media, key=lambda m: (m.media_type != "image", m.position, m.sha256)):
        tool = tool_for(m.media_type, m.mime_type)
        text, version = texts.get((m.sha256, tool or ""), ("", ""))
        text = text.strip()
        if not tool or not text or (m.sha256, m.media_type, m.position) in seen:
            continue
        seen.add((m.sha256, m.media_type, m.position))
        versions.add(version)
        heading = m.alt_text if (m.alt_text or "").startswith("Slide ") else None
        sections.append(f"## {heading}\n\n{text}" if heading else text)
    base = base_content(content)
    if not sections:
        return base, []
    return f"{base}\n\n{PART}\n\n" + "\n\n".join(sections), sorted(versions)


def with_text_from(metadata: dict[str, Any] | None, versions: list[str]) -> dict[str, Any]:
    """``metadata`` naming the tools that read its files' text (``text_from``)."""
    out = {k: v for k, v in (metadata or {}).items() if k != "text_from"}
    if versions:
        out["text_from"] = versions
    return out


def _current_media(conn: sqlite3.Connection, item_id: int) -> list[Media]:
    return [
        Media(*row)
        for row in conn.execute(
            "SELECT media_type, mime_type, position, alt_text, sha256 FROM item_media "
            "WHERE item_id = ? AND sha256 IS NOT NULL AND superseded_at IS NULL",
            (item_id,),
        )
    ]


def _texts(conn: sqlite3.Connection, media: Iterable[Media]) -> Texts:
    shas = sorted({m.sha256 for m in media})
    if not shas:
        return {}
    rows = conn.execute(
        "SELECT sha256, tool, text, tool_version FROM blob_text "
        f"WHERE sha256 IN ({','.join('?' * len(shas))})",
        shas,
    )
    return {(sha, tool): (text, version or "") for sha, tool, text, version in rows}


def content_on_import(
    conn: sqlite3.Connection,
    source_type: str,
    content: str | None,
    metadata: dict[str, Any] | None,
    media: Iterable[Any],
    item_id: int | None,
) -> tuple[str, dict[str, Any]] | None:
    """The content and metadata to store for an item being imported: its files' text
    added, from the media it has and the ones it brings. None if it gets no text."""
    if not wants_text(source_type, metadata):
        return None
    incoming = [
        Media(m.media_type, m.mime_type, m.position, m.alt_text, m.sha256)
        for m in media
        if getattr(m, "sha256", None)
    ]
    stored = _current_media(conn, item_id) if item_id is not None else []
    every = stored + incoming
    text, versions = compose(content, every, _texts(conn, every))
    return text, with_text_from(metadata, versions)


def refresh_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Rebuild one item's text from its current media; True if it changed. Does not
    commit."""
    row = conn.execute("SELECT content, metadata FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False
    media = _current_media(conn, item_id)
    new, versions = compose(row[0], media, _texts(conn, media))
    try:
        metadata = json.loads(row[1]) if row[1] else {}
    except ValueError:
        metadata = {}
    new_metadata = with_text_from(metadata, versions)
    if new == (row[0] or "") and new_metadata == metadata:
        return False
    conn.execute(
        "UPDATE items SET content = ?, metadata = ? WHERE id = ?",
        (new, json.dumps(new_metadata) if new_metadata else None, item_id),
    )
    return True


# --------------------------------------------------------------------------- #
# The background step


@dataclass
class ExtractCounts:
    files: int = 0  # files read this run
    failed: int = 0
    items: int = 0  # items whose text changed
    pending: int = 0  # files left for the next run (time budget)


def _items_wanting_text(conn: sqlite3.Connection) -> list[int]:
    kinds = sorted(ARTIFACT_KINDS)
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM items WHERE source_type = 'notebooklm' "
            "AND json_valid(metadata) AND json_extract(metadata, '$.type') = 'artifact' "
            f"AND json_extract(metadata, '$.artifact_type') IN ({','.join('?' * len(kinds))}) "
            "ORDER BY id",
            kinds,
        )
    ]


def extract_pending(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    budget_s: float | None = None,
) -> ExtractCounts:
    """Read each wanted file not read yet, then rebuild the items whose text changed.

    Files are read with no write open; each file's text is committed on its own, so a
    run stopped part-way keeps what it read. ``budget_s`` stops reading after that
    many seconds; the rest wait for the next run. Call it with no transaction open.
    """
    from lestash.core.database import mark_recent_history, max_history_id

    config = config or Config.load()
    store = BlobStore(store_root(config))
    counts = ExtractCounts()
    item_ids = _items_wanting_text(conn)
    todo: dict[tuple[str, str], str | None] = {}  # (sha, tool): mime
    for item_id in item_ids:
        for m in _current_media(conn, item_id):
            if tool := tool_for(m.media_type, m.mime_type):
                todo.setdefault((m.sha256, tool), m.mime_type)
    done = {
        (sha, tool)
        for sha, tool in conn.execute("SELECT sha256, tool FROM blob_text")
        if (sha, tool) in todo
    }
    started = time.monotonic()
    pending = [key for key in sorted(todo) if key not in done]
    for n, (sha, tool) in enumerate(pending):
        if budget_s is not None and time.monotonic() - started > budget_s:
            counts.pending = len(pending) - n
            break
        path = store.path(sha)
        try:
            text, version = read_file(tool, path)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            logger.warning("Could not read text from %s (%s): %s", path.name, tool, e)
            counts.failed += 1
            continue
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text) "
                "VALUES (?, ?, ?, ?)",
                (sha, tool, version, text),
            )
        counts.files += 1
    with conn:
        since = max_history_id(conn)
        changed = [i for i in item_ids if refresh_item(conn, i)]
        mark_recent_history(conn, since, "extract", item_ids=changed)
    counts.items = len(changed)
    return counts

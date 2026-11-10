"""Text from files that have none: NotebookLM slides, infographics, apps, audio and video
(#218, W3).

Text is read once per stored file and kept in ``blob_text`` (by hash and tool), so a
file in two items, or an item imported again, is never read twice. An item's text is
its own content, then a "## Text from the files" part built from its current media:
``## Slide 7`` and the slide's text for each slide, the text of an infographic, an
app's page text. The part is rebuilt from ``blob_text`` whenever the item is stored,
so an import keeps it and adds no history.

Slides and infographics are pictures: RapidOCR's English model reads them (the same
OCR Docling uses; Docling itself drops most of a slide's text as a picture). Apps are
web pages: their visible text is kept, and the texts in the data a NotebookLM app
draws itself from (``data-app-data``). Audio overviews and explainer videos are
transcribed by faster-whisper's ``small.en`` model, with a ``## 12:34`` heading about
each minute.
"""

from __future__ import annotations

import contextlib
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
WHISPER_MODEL = "small.en"
WHISPER_TOOL = f"faster-whisper-{WHISPER_MODEL}"
# The model folder lestash-voice uses too
WHISPER_MODELS = Path.home() / ".local" / "share" / "lestash" / "whisper-models"
HEADING_EVERY_S = 60  # a transcript's ``## 12:34`` headings, about this far apart
# Which items get text: NotebookLM artifacts of these kinds
ARTIFACT_KINDS = frozenset({"slides", "infographic", "app", "audio_overview", "explainer_video"})


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
    if media_type in ("audio", "video") and (mime_type or "").startswith(("audio/", "video/")):
        return WHISPER_TOOL
    return None


# --------------------------------------------------------------------------- #
# Reading files


class EngineUnavailable(Exception):
    """A tool's engine can't start (e.g. its model can't be fetched): no file can be read."""


def _package_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return "?"


def tool_version(tool: str) -> str:
    """The version of the tool that reads files now: a file that failed with another
    version is tried again."""
    if tool == OCR_TOOL:
        return f"rapidocr {_package_version('rapidocr')}, PP-OCRv5 en"
    if tool == WHISPER_TOOL:
        return f"faster-whisper {_package_version('faster-whisper')}, {WHISPER_MODEL}"
    return "html.parser"


class _Ocr:
    """RapidOCR, loaded once: its models take a second to load."""

    _engine: Any = None

    @classmethod
    def read(cls, path: Path) -> str:
        if cls._engine is None:
            # The default model is Chinese, and runs English words together. The
            # English one is fetched on first use, into rapidocr's models folder.
            try:
                from rapidocr import LangRec, OCRVersion, RapidOCR

                cls._engine = RapidOCR(
                    params={"Rec.lang_type": LangRec.EN, "Rec.ocr_version": OCRVersion.PPOCRV5}
                )
            except Exception as e:  # an import (cv2, onnxruntime) or the model fetch
                raise EngineUnavailable(f"RapidOCR could not start: {e}") from e
        result = cls._engine(str(path))
        return "\n".join(t.strip() for t in (result.txts or ()) if t.strip())


def clock(seconds: float) -> str:
    """``12:34``, or ``1:02:03`` from an hour on."""
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcript(segments: Iterable[tuple[float, str]], every: float = HEADING_EVERY_S) -> str:
    """A transcript from (start, text) segments: a ``## 12:34`` heading, then the text
    until the first segment that starts in a later ``every``-second span."""
    parts: list[str] = []
    span: int | None = None
    for start, text in segments:
        text = " ".join(text.split())
        if not text:
            continue
        if span is None or int(start // every) > span:
            parts.append(f"## {clock(start)}\n\n{text}")
            span = int(start // every)
        else:
            parts[-1] += f" {text}"
    return "\n\n".join(parts)


class _Whisper:
    """faster-whisper, loaded once: its model takes seconds to load."""

    _model: Any = None

    @classmethod
    def read(cls, path: Path) -> str:
        if cls._model is None:
            # The model is fetched on first use, into the folder lestash-voice uses
            try:
                from faster_whisper import WhisperModel

                cls._model = WhisperModel(
                    WHISPER_MODEL,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(WHISPER_MODELS),
                )
            except Exception as e:  # an import (ctranslate2, av) or the model fetch
                raise EngineUnavailable(f"faster-whisper could not start: {e}") from e
        # The voice-activity filter skips music and silence, where whisper makes up words
        segments, _ = cls._model.transcribe(str(path), vad_filter=True)
        return transcript((seg.start, seg.text) for seg in segments)


class _PageText(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        for name, value in attrs:
            # A NotebookLM app draws its page with scripts from data in this attribute:
            # flashcards, mind maps, quizzes
            if name == "data-app-data" and value:
                with contextlib.suppress(ValueError, RecursionError):  # nested too deep
                    self.parts.extend(_strings(json.loads(value)))

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self.parts.append(" ".join(data.split()))


def _strings(value: Any) -> list[str]:
    """Every text in a JSON value, in order, one per line."""
    if isinstance(value, str):
        text = " ".join(value.split())
        return [text] if text else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in _strings(v)]
    if isinstance(value, list):
        return [t for v in value for t in _strings(v)]
    return []


def page_text(path: Path) -> str:
    """A web page's visible text, one run of text per line. Only parsed, never run."""
    parser = _PageText()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return "\n".join(parser.parts)


def read_file(tool: str, path: Path) -> tuple[str, str]:
    """The text of a file and the tool's version."""
    if tool == OCR_TOOL:
        return _Ocr.read(path), tool_version(tool)
    if tool == HTML_TOOL:
        return page_text(path), tool_version(tool)
    if tool == WHISPER_TOOL:
        return _Whisper.read(path), tool_version(tool)
    raise ValueError(f"unknown tool {tool}")


def _failed(version: str) -> str:
    return f"failed [{version}]"


# --------------------------------------------------------------------------- #
# Building an item's text


@dataclass(frozen=True)
class Media:
    media_type: str
    mime_type: str | None
    position: int
    alt_text: str | None
    sha256: str
    order: int = 1 << 62  # its row id: a later version is newer; one being imported newest


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
    # One file per place: a deck made again keeps its old slides as media too, and
    # only the newest at each position is read into the text
    newest: dict[tuple[str, int, str], Media] = {}
    for m in media:
        tool = tool_for(m.media_type, m.mime_type)
        if tool is None:
            continue
        key = (m.media_type, m.position, tool)
        if key not in newest or m.order > newest[key].order:
            newest[key] = m
    for m in sorted(newest.values(), key=lambda m: (m.media_type != "image", m.position)):
        tool = tool_for(m.media_type, m.mime_type) or ""
        text, version = texts.get((m.sha256, tool), ("", ""))
        text = text.strip()
        if not text:
            continue
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
            "SELECT media_type, mime_type, position, alt_text, sha256, id FROM item_media "
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
        # In the order they come, after every stored row
        Media(m.media_type, m.mime_type, m.position, m.alt_text, m.sha256, (1 << 62) + n)
        for n, m in enumerate(m for m in media if getattr(m, "sha256", None))
    ]
    stored = _current_media(conn, item_id) if item_id is not None else []
    # An incoming file already stored keeps its row's place among the versions
    known = {(m.media_type, m.position, m.sha256) for m in stored}
    every = stored + [m for m in incoming if (m.media_type, m.position, m.sha256) not in known]
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
    failed: int = 0  # files that could not be read: recorded, tried again by a new version
    stopped: str | None = None  # why the run stopped early: the rest wait
    engine_failed: bool = False  # the tool itself is broken, not one file
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


def _still_reads(conn: sqlite3.Connection, store: BlobStore, tool: str) -> bool | None:
    """Whether the tool reads again a file it read before: if so, files that fail are
    bad, not the tool. None if it has read no file still in the store."""
    # The smallest first: transcribing a long recording takes minutes
    for (sha,) in conn.execute(
        "SELECT t.sha256 FROM blob_text t LEFT JOIN blobs b ON b.sha256 = t.sha256 "
        "WHERE t.tool = ? AND t.tool_version NOT LIKE 'failed [%' "
        "ORDER BY coalesce(b.size, 0) = 0, b.size, t.extracted_at DESC",  # 0: size unknown
        (tool,),
    ):
        path = store.path(sha)
        if path.is_file():
            try:
                read_file(tool, path)
            except Exception:  # noqa: BLE001 - any failure: the tool is broken
                return False
            return True
    return None


def extract_pending(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    budget_s: float | None = None,
    retry_failed: bool = False,
) -> ExtractCounts:
    """Read each wanted file not read yet, then rebuild the items whose text changed.

    Files are read with no write open; each file's text is committed on its own, so a
    run stopped part-way keeps what it read. ``budget_s`` stops reading after that
    many seconds; the rest wait for the next run. Call it with no transaction open.

    A file that fails is recorded, and read again once the tool's version changes,
    or with ``retry_failed``. Failures are only recorded once a file in the run has
    been read: a run whose first reads all fail stops, as the tool is broken.
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
    done = set()
    for sha, tool, version in conn.execute("SELECT sha256, tool, tool_version FROM blob_text"):
        if (sha, tool) not in todo:
            continue
        version = version or ""
        if version.startswith("failed [") and (
            retry_failed or not version.startswith(_failed(tool_version(tool)))
        ):
            continue  # failed with another version: try again
        done.add((sha, tool))
    started = time.monotonic()
    # Transcripts last: they are slow, and a recording that kills the run then stops
    # no picture or page from being read
    pending = sorted(
        (key for key in todo if key not in done), key=lambda k: (k[1] == WHISPER_TOOL, k)
    )
    # Failures of a tool are kept back until that tool reads a file: if it reads none,
    # the tool is broken, not the files, and they are read again next run
    held: dict[str, list[tuple[str, str, str, str]]] = {}
    good: set[str] = set()
    # A tool whose engine can't start: its files wait, and the other tools go on
    unavailable: dict[str, str] = {}
    waiting = 0  # files of an unavailable tool

    def write(rows: list[tuple[str, str, str, str]]) -> bool:
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO blob_text (sha256, tool, tool_version, text) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.OperationalError as e:  # a sync holds the lock: go on next run
            counts.stopped = f"database: {e}"
            return False
        return True

    for n, (sha, tool) in enumerate(pending):
        if budget_s is not None and time.monotonic() - started > budget_s:
            counts.pending = len(pending) - n
            break
        if tool in unavailable:
            waiting += 1
            continue
        path = store.path(sha)
        if not path.is_file():
            logger.warning("Not in the file store: %s", sha)  # not the file's fault
            continue
        try:
            text, version = read_file(tool, path)
        except EngineUnavailable as e:  # every file of this tool would fail
            logger.warning("%s", e)
            unavailable[tool] = str(e)
            held.pop(tool, None)
            waiting += 1
            continue
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            logger.warning("Could not read text from %s (%s): %s", path.name, tool, e)
            failure = (
                sha,
                tool,
                f"{_failed(tool_version(tool))}: {type(e).__name__}: {e}"[:300],
                "",
            )
            counts.failed += 1
            if tool in good:
                if not write([failure]):
                    break
            else:
                held.setdefault(tool, []).append(failure)
            continue
        counts.files += 1
        rows = [*held.pop(tool, []), (sha, tool, version, text)]
        good.add(tool)
        if not write(rows):
            break
    if not counts.stopped and not counts.pending:  # judged only on a whole run
        broken = []
        for tool, rows in sorted(held.items()):
            reads = _still_reads(conn, store, tool)
            if reads is False or (reads is None and len(rows) >= 3):
                broken.append(tool)
            elif not write(rows):  # the files, not the tool: record them
                break
        if broken and not counts.stopped:
            counts.stopped = f"{', '.join(broken)} read no file and failed on every one"
            counts.engine_failed = True
    counts.pending += waiting  # after the judging above, which a budget stop skips
    if unavailable:
        counts.engine_failed = True
        counts.stopped = "; ".join([*unavailable.values(), *filter(None, [counts.stopped])])
    try:
        conn.execute("BEGIN IMMEDIATE")  # read and write each item under one lock
        try:
            since = max_history_id(conn)
            changed = [i for i in item_ids if refresh_item(conn, i)]
            mark_recent_history(conn, since, "extract", item_ids=changed)
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
    except sqlite3.OperationalError as e:
        counts.stopped = "; ".join(filter(None, [counts.stopped, f"database: {e}"]))
        return counts
    counts.items = len(changed)
    return counts

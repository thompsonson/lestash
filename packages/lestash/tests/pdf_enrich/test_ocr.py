"""Tests for the Claude-vision OCR pass.

The `anthropic` SDK is mocked at the client boundary — no network calls.
Stroke rendering is exercised against real PyMuPDF.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import (
    add_item_media,
    get_connection,
    init_database,
    save_media_file,
    upsert_item,
)
from lestash.core.pdf_enrich import ocr as ocr_module
from lestash.core.pdf_enrich.ocr import (
    OCR_EXTRACTOR_VERSION,
    ocr_annotation,
    ocr_pending_annotations,
)
from lestash.models.item import ItemCreate


@pytest.fixture
def config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(general=GeneralConfig(database_path=str(Path(tmp) / "t.db")))
        init_database(cfg)
        yield cfg


@pytest.fixture
def annotated_item(config: Config, make_pdf):
    """A parent PDF item with one margin_note child needing OCR."""
    pdf_path = make_pdf([{"text": [((50, 50, 500, 100), "body of doc")]}], name="parent.pdf")
    pdf_bytes = pdf_path.read_bytes()

    with get_connection(config) as conn:
        parent_id = upsert_item(
            conn,
            ItemCreate(
                source_type="pdf",
                source_id="parent-1",
                title="Parent PDF",
                content="body",
            ),
        )
        rel = save_media_file(parent_id, pdf_bytes, "parent.pdf", config=config)
        add_item_media(
            conn,
            parent_id,
            media_type="source_pdf",
            local_path=rel,
            mime_type="application/pdf",
        )
        # Margin note child
        child_id = conn.execute(
            """
            INSERT INTO items (source_type, source_id, title, content, metadata, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "pdf_annotation",
                "child-1",
                "Margin note",
                "ink",
                json.dumps(
                    {
                        "annotation_kind": "margin_note",
                        "page": 0,
                        "bbox": [10, 60, 60, 90],
                        "color": "#000000",
                        "strokes": [[[12, 65], [55, 70]]],
                        "stroke_geometry_hash": "h" * 64,
                        "annotation_id": "mn-1",
                    }
                ),
                parent_id,
            ),
        ).lastrowid
        conn.commit()
    assert child_id is not None
    return parent_id, child_id


def _mock_anthropic(text: str):
    """Patch ocr._transcribe_with_claude to return `text` without touching
    the SDK or environment at all."""
    return patch.object(ocr_module, "_transcribe_with_claude", return_value=text)


def test_ocr_writes_transcription_to_child_content(config, annotated_item):
    parent_id, child_id = annotated_item
    with _mock_anthropic("Add a title page"):
        result = ocr_annotation(child_id, config=config)
    assert result.status == "transcribed"
    assert result.text == "Add a title page"

    with get_connection(config) as conn:
        row = conn.execute(
            "SELECT content, metadata FROM items WHERE id = ?", (child_id,)
        ).fetchone()
    assert row["content"] == "Add a title page"
    meta = json.loads(row["metadata"])
    assert meta["ocr_text"] == "Add a title page"
    assert meta["ocr_version"] == OCR_EXTRACTOR_VERSION


def test_ocr_is_idempotent_when_version_matches(config, annotated_item):
    parent_id, child_id = annotated_item
    with _mock_anthropic("first run") as patched:
        first = ocr_annotation(child_id, config=config)
        second = ocr_annotation(child_id, config=config)
    assert first.status == "transcribed"
    assert second.status == "skipped"
    # Mock should have been called exactly once
    assert patched.call_count == 1


def test_ocr_skips_kinds_outside_target(config, annotated_item):
    parent_id, child_id = annotated_item
    # Promote the child to an underline (not in OCR_TARGET_KINDS)
    with get_connection(config) as conn:
        meta = json.loads(
            conn.execute("SELECT metadata FROM items WHERE id = ?", (child_id,)).fetchone()[0]
        )
        meta["annotation_kind"] = "underline"
        conn.execute(
            "UPDATE items SET metadata = ? WHERE id = ?",
            (json.dumps(meta), child_id),
        )
        conn.commit()

    with _mock_anthropic("never called") as patched:
        result = ocr_annotation(child_id, config=config)
    assert result.status == "skipped"
    assert patched.call_count == 0


def test_ocr_unavailable_when_source_pdf_missing(config):
    """A child whose parent has no source_pdf media row → unavailable, not crash."""
    with get_connection(config) as conn:
        parent_id = upsert_item(
            conn,
            ItemCreate(
                source_type="pdf",
                source_id="orphan-parent",
                title="orphan",
                content="x",
            ),
        )
        child_id = conn.execute(
            "INSERT INTO items (source_type, source_id, content, metadata, parent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "pdf_annotation",
                "orphan-child",
                "ink",
                json.dumps(
                    {
                        "annotation_kind": "margin_note",
                        "page": 0,
                        "bbox": [10, 10, 50, 50],
                        "stroke_geometry_hash": "z" * 64,
                    }
                ),
                parent_id,
            ),
        ).lastrowid
        conn.commit()

    result = ocr_annotation(child_id, config=config)
    assert result.status == "unavailable"


def test_ocr_pending_annotations_filters_by_parent(config, annotated_item):
    parent_id, child_id = annotated_item
    # Create a second parent with its own margin note that should NOT be touched
    with get_connection(config) as conn:
        other_parent = upsert_item(
            conn,
            ItemCreate(source_type="pdf", source_id="other-parent", content="x"),
        )
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, metadata, parent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "pdf_annotation",
                "other-child",
                "ink",
                json.dumps(
                    {
                        "annotation_kind": "margin_note",
                        "page": 0,
                        "bbox": [10, 10, 50, 50],
                        "stroke_geometry_hash": "y" * 64,
                    }
                ),
                other_parent,
            ),
        )
        conn.commit()

    with _mock_anthropic("transcribed"):
        results = ocr_pending_annotations(item_id=parent_id, config=config)

    assert len(results) == 1
    assert results[0].child_id == child_id


def test_ocr_unreadable_response_yields_empty_string(config, annotated_item):
    parent_id, child_id = annotated_item
    # The function under _transcribe_with_claude maps "UNREADABLE" → ""; we
    # mock at the same boundary and test the behaviour at the public edge.
    with patch.object(ocr_module, "_transcribe_with_claude", return_value=""):
        result = ocr_annotation(child_id, config=config)
    assert result.status == "transcribed"
    assert result.text == ""

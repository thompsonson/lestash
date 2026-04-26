"""Tests for `enrich_item` runner — the integration glue between extractor,
persistence, and source-PDF resolution."""

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
from lestash.core.pdf_enrich.runner import enrich_item
from lestash.core.pdf_enrich.types import (
    EnrichedPdf,
    ExtractedAnnotation,
    ExtractedImage,
)
from lestash.models.item import ItemCreate


@pytest.fixture
def config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(general=GeneralConfig(database_path=str(Path(tmp) / "t.db")))
        init_database(cfg)
        yield cfg


@pytest.fixture
def item_with_local_source_pdf(config: Config, make_pdf):
    """An item with a real (synthesised) source_pdf media row pointing at a
    PDF on disk."""
    pdf_path = make_pdf([{"text": [((50, 50, 500, 100), "hello pdf")]}], name="src.pdf")
    pdf_bytes = pdf_path.read_bytes()

    with get_connection(config) as conn:
        item_id = upsert_item(
            conn,
            ItemCreate(
                source_type="google-drive",
                source_id="drive-1",
                title="Item One",
                content="raw docling content",
                metadata={"drive_id": "drive-1", "mime_type": "application/pdf"},
            ),
        )
        rel = save_media_file(item_id, pdf_bytes, "src.pdf", config=config)
        add_item_media(
            conn,
            item_id,
            media_type="source_pdf",
            local_path=rel,
            mime_type="application/pdf",
        )
    return item_id


def _stub_extractor(enriched: EnrichedPdf):
    return patch("lestash.core.pdf_enrich.runner.enrich_pdf", return_value=enriched)


def test_enrich_item_happy_path(config, item_with_local_source_pdf):
    enriched = EnrichedPdf(
        content="enriched content with [link](http://x)",
        pdf_sha256="a" * 64,
        extractor_version=1,
        images=[],
        annotations=[
            ExtractedAnnotation(
                kind="underline",
                page=0,
                bbox=(100, 200, 300, 220),
                anchor_text="hello pdf",
                color="#000000",
                strokes=[[(100, 200), (300, 200)]],
                annotation_id="u-1",
                created_at=None,
                stroke_geometry_hash="h" * 64,
            )
        ],
    )
    with _stub_extractor(enriched):
        result = enrich_item(item_with_local_source_pdf, config=config)
    assert result.status == "enriched"
    assert result.annotations == 1

    with get_connection(config) as conn:
        item = conn.execute(
            "SELECT content, metadata FROM items WHERE id = ?",
            (item_with_local_source_pdf,),
        ).fetchone()
    assert item["content"] == "enriched content with [link](http://x)"
    meta = json.loads(item["metadata"])
    assert meta["pdf_sha256"] == "a" * 64


def test_enrich_item_skips_when_already_enriched(config, item_with_local_source_pdf):
    enriched = EnrichedPdf(
        content="run-1",
        pdf_sha256="b" * 64,
        extractor_version=1,
    )
    with _stub_extractor(enriched):
        first = enrich_item(item_with_local_source_pdf, config=config)
        second = enrich_item(item_with_local_source_pdf, config=config)
    assert first.status == "enriched"
    assert second.status == "skipped"


def test_enrich_item_force_reruns_even_when_match(config, item_with_local_source_pdf):
    enriched = EnrichedPdf(
        content="run-1",
        pdf_sha256="b" * 64,
        extractor_version=1,
    )
    with _stub_extractor(enriched):
        enrich_item(item_with_local_source_pdf, config=config)
        forced = enrich_item(item_with_local_source_pdf, config=config, force=True)
    assert forced.status == "enriched"


def test_enrich_item_marks_source_unavailable_when_no_pdf(config):
    """Item with neither a source_pdf media row nor a drive_id → unavailable."""
    with get_connection(config) as conn:
        item_id = upsert_item(
            conn,
            ItemCreate(
                source_type="google-drive",
                source_id="orphan-1",
                title="orphan",
                content="some content",
                metadata={"mime_type": "application/pdf"},
            ),
        )
    result = enrich_item(item_id, config=config)
    assert result.status == "source_unavailable"

    with get_connection(config) as conn:
        meta = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    assert json.loads(meta)["enrichment_status"] == "source_unavailable"


def test_enrich_item_returns_failed_for_missing_item(config):
    result = enrich_item(999_999, config=config)
    assert result.status == "failed"


def test_enrich_item_returns_failed_when_extractor_raises(config, item_with_local_source_pdf):
    with patch(
        "lestash.core.pdf_enrich.runner.enrich_pdf",
        side_effect=RuntimeError("boom"),
    ):
        result = enrich_item(item_with_local_source_pdf, config=config)
    assert result.status == "failed"
    assert "boom" in (result.message or "")


def test_extracted_image_count_propagates(config, item_with_local_source_pdf):
    enriched = EnrichedPdf(
        content="<!-- image -->",
        pdf_sha256="c" * 64,
        extractor_version=1,
        images=[
            ExtractedImage(
                placeholder_index=0,
                page=0,
                bbox=(0, 0, 10, 10),
                bytes_=b"png-bytes",
                mime_type="image/png",
                xref_hash="x" * 64,
            )
        ],
    )
    with _stub_extractor(enriched):
        result = enrich_item(item_with_local_source_pdf, config=config)
    assert result.images == 1

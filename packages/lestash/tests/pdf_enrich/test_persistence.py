"""Tests for the persistence layer.

Drive an `EnrichedPdf` artifact directly into a real SQLite database and
verify the resulting rows. The extractor is not exercised here.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import (
    add_item_media,
    get_connection,
    init_database,
    upsert_item,
)
from lestash.core.pdf_enrich.persistence import (
    is_already_enriched,
    mark_source_unavailable,
    persist_enrichment,
)
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
def parent_pdf_item(config: Config) -> int:
    with get_connection(config) as conn:
        item_id = upsert_item(
            conn,
            ItemCreate(
                source_type="google-drive",
                source_id="drive-fixture-1",
                title="A Document",
                content="<!-- image -->\noriginal docling output",
                metadata={"drive_id": "drive-fixture-1", "mime_type": "application/pdf"},
            ),
        )
    return item_id


def _enriched(content: str = "<!-- image -->\nbody", **kwargs) -> EnrichedPdf:
    return EnrichedPdf(
        content=content,
        pdf_sha256=kwargs.get("sha", "a" * 64),
        extractor_version=kwargs.get("version", 1),
        images=kwargs.get("images", []),
        annotations=kwargs.get("annotations", []),
    )


def _image(idx: int = 0) -> ExtractedImage:
    return ExtractedImage(
        placeholder_index=idx,
        page=0,
        bbox=(0, 0, 10, 10),
        bytes_=b"fake-png-bytes",
        mime_type="image/png",
        xref_hash="abc" * 21 + "d",
    )


def _annotation(kind: str = "underline", anchor: str = "key phrase") -> ExtractedAnnotation:
    return ExtractedAnnotation(
        kind=kind,  # type: ignore[arg-type]
        page=0,
        bbox=(100, 200, 300, 220),
        anchor_text=anchor,
        color="#ff0000",
        strokes=[[(100, 200), (300, 200)]],
        annotation_id="annot-uuid-1",
        created_at="D:20260415120000Z",
        stroke_geometry_hash="h" * 64,
    )


def test_persist_writes_image_media_row_and_rewrites_content(config, parent_pdf_item):
    enriched = _enriched(content="<!-- image -->\nbody", images=[_image(0)])
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, enriched)

        rows = conn.execute(
            "SELECT media_type, source_origin FROM item_media WHERE item_id = ?",
            (parent_pdf_item,),
        ).fetchall()
        assert any(r["media_type"] == "image" and r["source_origin"] == "enricher" for r in rows)

        item = conn.execute("SELECT content FROM items WHERE id = ?", (parent_pdf_item,)).fetchone()
    assert "/api/media/" in item["content"]
    assert "<!-- image -->" not in item["content"]


def test_persist_creates_child_annotation_items(config, parent_pdf_item):
    enriched = _enriched(annotations=[_annotation()])
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, enriched)
        children = conn.execute(
            "SELECT source_type, content, metadata, parent_id FROM items WHERE parent_id = ?",
            (parent_pdf_item,),
        ).fetchall()
    assert len(children) == 1
    child = children[0]
    assert child["source_type"] == "pdf_annotation"
    assert child["content"] == "key phrase"
    meta = json.loads(child["metadata"])
    assert meta["annotation_kind"] == "underline"
    assert meta["page"] == 0
    assert meta["color"] == "#ff0000"


def test_persist_updates_parent_metadata_with_sha_and_version(config, parent_pdf_item):
    enriched = _enriched(sha="cafe" * 16, version=7)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, enriched)
        row = conn.execute("SELECT metadata FROM items WHERE id = ?", (parent_pdf_item,)).fetchone()
    meta = json.loads(row["metadata"])
    assert meta["pdf_sha256"] == "cafe" * 16
    assert meta["extractor_version"] == 7
    assert meta["enrichment_status"] == "ok"
    assert meta["enriched_at"]


def test_idempotent_rerun_replaces_prior_artifacts(config, parent_pdf_item):
    first = _enriched(images=[_image(0)], annotations=[_annotation(kind="underline")])
    second = _enriched(
        images=[],
        annotations=[_annotation(kind="circle", anchor="other phrase")],
    )

    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, first)
        persist_enrichment(conn, config, parent_pdf_item, second)

        media_count = conn.execute(
            "SELECT COUNT(*) FROM item_media WHERE item_id = ? AND source_origin = 'enricher'",
            (parent_pdf_item,),
        ).fetchone()[0]
        children = conn.execute(
            "SELECT metadata FROM items WHERE parent_id = ? AND source_type = 'pdf_annotation'",
            (parent_pdf_item,),
        ).fetchall()
    assert media_count == 0  # second run had no images; first run's image was deleted
    assert len(children) == 1
    assert json.loads(children[0]["metadata"])["annotation_kind"] == "circle"


def test_repeated_image_dedups_to_one_media_row_but_replaces_all_placeholders(
    config, parent_pdf_item
):
    """Regression for #143: the same image (same xref_hash) appearing on
    multiple pages must produce one media row whose ID is reused for every
    placeholder, not just the first.
    """
    shared_hash = "share" * 12 + "share1234"  # 64 chars
    images = [
        ExtractedImage(
            placeholder_index=i,
            page=i,
            bbox=(0, 0, 10, 10),
            bytes_=b"identical-png-bytes",
            mime_type="image/png",
            xref_hash=shared_hash,
        )
        for i in range(3)
    ]
    enriched = _enriched(
        content="<!-- image -->\nA\n<!-- image -->\nB\n<!-- image -->\n",
        images=images,
    )

    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, enriched)
        media_rows = conn.execute(
            "SELECT id FROM item_media WHERE item_id = ? AND source_origin = 'enricher'",
            (parent_pdf_item,),
        ).fetchall()
        item = conn.execute("SELECT content FROM items WHERE id = ?", (parent_pdf_item,)).fetchone()

    assert len(media_rows) == 1
    media_id = media_rows[0]["id"]
    # All three placeholders should have been substituted with the same
    # media_id link, leaving zero `<!-- image -->` markers behind.
    assert "<!-- image -->" not in item["content"]
    assert item["content"].count(f"/api/media/{media_id}") == 3


def test_persist_does_not_delete_source_pdf_media_row(config, parent_pdf_item):
    with get_connection(config) as conn:
        add_item_media(
            conn,
            parent_pdf_item,
            media_type="source_pdf",
            local_path="42/abc.pdf",
            mime_type="application/pdf",
            source_origin="sync",
        )
        persist_enrichment(conn, config, parent_pdf_item, _enriched())
        survivors = conn.execute(
            "SELECT media_type FROM item_media WHERE item_id = ?",
            (parent_pdf_item,),
        ).fetchall()
    assert any(r["media_type"] == "source_pdf" for r in survivors)


def test_is_already_enriched(config, parent_pdf_item):
    enriched = _enriched(sha="d" * 64, version=3)
    with get_connection(config) as conn:
        assert not is_already_enriched(conn, parent_pdf_item, 3, "d" * 64)
        persist_enrichment(conn, config, parent_pdf_item, enriched)
        assert is_already_enriched(conn, parent_pdf_item, 3, "d" * 64)
        assert not is_already_enriched(conn, parent_pdf_item, 4, "d" * 64)
        assert not is_already_enriched(conn, parent_pdf_item, 3, "e" * 64)


def test_mark_source_unavailable_preserves_content(config, parent_pdf_item):
    with get_connection(config) as conn:
        mark_source_unavailable(conn, parent_pdf_item)
        row = conn.execute(
            "SELECT content, metadata FROM items WHERE id = ?", (parent_pdf_item,)
        ).fetchone()
    assert "original docling output" in row["content"]
    meta = json.loads(row["metadata"])
    assert meta["enrichment_status"] == "source_unavailable"


def test_persist_rolls_back_on_failure(config, parent_pdf_item, monkeypatch):
    """If persistence raises mid-transaction, prior state must be untouched."""
    # First a successful run for a baseline
    baseline = _enriched(annotations=[_annotation(anchor="baseline phrase")])
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, baseline)
        baseline_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE parent_id = ?", (parent_pdf_item,)
        ).fetchone()[0]
    assert baseline_count == 1

    # Now break image storage and confirm rollback
    from lestash.core.pdf_enrich import persistence

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(persistence, "save_media_file", boom)

    bad = _enriched(images=[_image(0)])
    with get_connection(config) as conn:
        with pytest.raises(RuntimeError):
            persist_enrichment(conn, config, parent_pdf_item, bad)

        # Baseline child must still be present — rollback worked
        survivors = conn.execute(
            "SELECT COUNT(*) FROM items WHERE parent_id = ?", (parent_pdf_item,)
        ).fetchone()[0]
    assert survivors == baseline_count

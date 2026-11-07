"""Re-enriching a PDF that changed (#196).

A Kobo PDF gains ink between plug-ins, so its sha256 changes and the same
item is enriched again. These tests drive real PDFs through
`attach_source_pdf_and_enrich` (Docling stubbed out) and check that:

- annotation children keep their item IDs and OCR text,
- only new annotations are added and only removed ones are deleted,
- the item keeps exactly one `source_pdf` row, with earlier versions in metadata.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.core.pdf_enrich import ocr as ocr_module
from lestash.core.pdf_enrich.ocr import OCR_EXTRACTOR_VERSION, ocr_pending_annotations
from lestash.core.pdf_enrich.runner import attach_source_pdf_and_enrich
from lestash.models.item import ItemCreate

# Right-margin scribbles (classified as margin_note, so OCR-eligible), far
# enough apart that each becomes its own annotation group.
MARGIN_YS = (100, 250, 400)
NEW_Y = 600


def _scribble(y: float) -> list[tuple[float, float]]:
    return [(520, y), (530, y + 8), (540, y), (550, y + 8), (560, y)]


@pytest.fixture
def config() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(general=GeneralConfig(database_path=str(Path(tmp) / "t.db")))
        init_database(cfg)
        yield cfg


@pytest.fixture(autouse=True)
def _no_docling():
    with patch(
        "lestash.core.pdf_enrich.extractor.convert_to_markdown",
        return_value="document body",
    ):
        yield


@pytest.fixture
def item_id(config: Config) -> int:
    with get_connection(config) as conn:
        return upsert_item(
            conn,
            ItemCreate(
                source_type="kobo",
                source_id="kobo-book-1",
                title="Annotated book",
                content="document body",
            ),
        )


def _base_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(50, 50, 450, 100), "body text", fontsize=11)
    for y in MARGIN_YS:
        annot = page.add_ink_annot([_scribble(y)])
        annot.update()
    data = doc.tobytes()
    doc.close()
    return data


def _add_ink(pdf: bytes, stroke: list[tuple[float, float]]) -> bytes:
    """Return `pdf` with one more ink annotation; existing ones keep their /NM."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    annot = doc[0].add_ink_annot([stroke])
    annot.update()
    data = doc.tobytes()
    doc.close()
    return data


def _drop_ink_at(pdf: bytes, y: float) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    page = doc[0]
    for annot in list(page.annots()):
        if annot.type[1] == "Ink" and annot.rect.y0 <= y <= annot.rect.y1:
            page.delete_annot(annot)
    data = doc.tobytes()
    doc.close()
    return data


def _children(config: Config, parent_id: int) -> dict[int, dict]:
    with get_connection(config) as conn:
        rows = conn.execute(
            "SELECT id, content, metadata FROM items WHERE parent_id = ? "
            "AND source_type = 'pdf_annotation'",
            (parent_id,),
        ).fetchall()
    return {row["id"]: {"content": row["content"], **json.loads(row["metadata"])} for row in rows}


def _by_y(children: dict[int, dict]) -> dict[int, int]:
    """Map each child's rounded top y to its item ID."""
    return {round(child["bbox"][1] / 50) * 50: cid for cid, child in children.items()}


def _ocr_everything(config: Config, parent_id: int) -> list[int]:
    """Run the OCR pass with a fake transcriber; return the child IDs it billed."""
    with patch.object(ocr_module, "_transcribe_with_claude", side_effect=lambda png: "note"):
        results = ocr_pending_annotations(item_id=parent_id, config=config)
    return [r.child_id for r in results if r.status == "transcribed"]


def _source_pdf_rows(config: Config, parent_id: int) -> list:
    with get_connection(config) as conn:
        return conn.execute(
            "SELECT id, local_path FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (parent_id,),
        ).fetchall()


def _parent_meta(config: Config, parent_id: int) -> dict:
    with get_connection(config) as conn:
        row = conn.execute("SELECT metadata FROM items WHERE id = ?", (parent_id,)).fetchone()
    return json.loads(row["metadata"])


def test_new_stroke_keeps_existing_children_and_their_ocr(config, item_id):
    v1 = _base_pdf()
    assert attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config).annotations == 3
    before = _children(config, item_id)
    assert {c["annotation_kind"] for c in before.values()} == {"margin_note"}
    assert sorted(_ocr_everything(config, item_id)) == sorted(before)

    v2 = _add_ink(v1, _scribble(NEW_Y))
    result = attach_source_pdf_and_enrich(item_id, v2, "book.pdf", config=config)
    assert result.status == "enriched"
    after = _children(config, item_id)

    assert len(after) == 4
    assert set(before) <= set(after)
    for cid in before:
        assert after[cid]["content"] == "note"
        assert after[cid]["ocr_text"] == "note"
        assert after[cid]["ocr_version"] == OCR_EXTRACTOR_VERSION
    (new_id,) = set(after) - set(before)
    assert "ocr_version" not in after[new_id]

    # The next OCR pass pays for the new annotation only.
    assert _ocr_everything(config, item_id) == [new_id]


def test_stroke_joining_an_annotation_refreshes_only_that_child(config, item_id):
    v1 = _base_pdf()
    attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config)
    _ocr_everything(config, item_id)
    before = _children(config, item_id)
    target = _by_y(before)[100]

    # A stroke right next to the y=100 scribble joins its group.
    v2 = _add_ink(v1, _scribble(110))
    attach_source_pdf_and_enrich(item_id, v2, "book.pdf", config=config)
    after = _children(config, item_id)

    assert set(after) == set(before)
    assert after[target]["stroke_geometry_hash"] != before[target]["stroke_geometry_hash"]
    assert "ocr_version" not in after[target]
    assert after[target]["content"] != "note"
    for cid in set(before) - {target}:
        assert after[cid]["ocr_text"] == "note"
    assert _ocr_everything(config, item_id) == [target]


def test_removed_annotation_deletes_only_its_child(config, item_id):
    v1 = _base_pdf()
    attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config)
    _ocr_everything(config, item_id)
    before = _children(config, item_id)
    gone = _by_y(before)[250]

    v2 = _drop_ink_at(v1, 255)
    attach_source_pdf_and_enrich(item_id, v2, "book.pdf", config=config)
    after = _children(config, item_id)

    assert set(after) == set(before) - {gone}
    assert all(c["ocr_text"] == "note" for c in after.values())


def _versions(config: Config, parent_id: int) -> list:
    with get_connection(config) as conn:
        return conn.execute(
            "SELECT local_path, created_at FROM item_media "
            "WHERE item_id = ? AND media_type = 'source_pdf_version' ORDER BY position",
            (parent_id,),
        ).fetchall()


def _resync(config: Config) -> None:
    """What a Drive or Kobo re-sync does first: upsert, which replaces the metadata."""
    with get_connection(config) as conn:
        upsert_item(
            conn,
            ItemCreate(
                source_type="kobo",
                source_id="kobo-book-1",
                title="Annotated book",
                content="document body",
                metadata={"mime_type": "application/pdf"},
            ),
        )


def test_new_pdf_version_keeps_one_source_pdf_row(config, item_id):
    v1 = _base_pdf()
    attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config)
    (row_v1,) = _source_pdf_rows(config, item_id)

    v2 = _add_ink(v1, _scribble(NEW_Y))
    attach_source_pdf_and_enrich(item_id, v2, "book.pdf", config=config)
    (row_v2,) = _source_pdf_rows(config, item_id)

    assert row_v2["id"] == row_v1["id"]
    assert row_v2["local_path"] != row_v1["local_path"]
    assert _parent_meta(config, item_id)["pdf_sha256"] == hashlib.sha256(v2).hexdigest()
    (prev,) = _versions(config, item_id)
    assert prev["local_path"] == row_v1["local_path"]
    assert prev["created_at"]


def test_version_history_survives_resync(config, item_id):
    """A re-sync replaces the item's metadata, so the history must live elsewhere."""
    v1 = _base_pdf()
    v2 = _add_ink(v1, _scribble(NEW_Y))
    v3 = _add_ink(v2, _scribble(NEW_Y + 100))
    paths = []
    for pdf in (v1, v2, v3):
        _resync(config)
        attach_source_pdf_and_enrich(item_id, pdf, "book.pdf", config=config)
        paths.append(_source_pdf_rows(config, item_id)[0]["local_path"])

    assert [v["local_path"] for v in _versions(config, item_id)] == paths[:2]


def test_going_back_to_an_earlier_pdf_records_every_step(config, item_id):
    a = _base_pdf()
    b = _add_ink(a, _scribble(NEW_Y))
    for pdf in (a, b, a, b):
        attach_source_pdf_and_enrich(item_id, pdf, "book.pdf", config=config)

    (path_b,) = [r["local_path"] for r in _source_pdf_rows(config, item_id)]
    history = [v["local_path"] for v in _versions(config, item_id)]
    assert len(history) == 3
    assert history[1] == path_b
    assert history[0] == history[2] != path_b


def test_reattaching_the_same_pdf_records_no_version(config, item_id):
    v1 = _base_pdf()
    attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config)
    attach_source_pdf_and_enrich(item_id, v1, "book.pdf", config=config)

    assert len(_source_pdf_rows(config, item_id)) == 1
    assert _versions(config, item_id) == []


def test_same_annotation_ids_in_two_pdfs_do_not_collide(config, item_id):
    """PyMuPDF names annotations fitz-A0, fitz-A1, ... in every file, so
    child source_ids must be scoped to the parent."""
    with get_connection(config) as conn:
        other_id = upsert_item(
            conn,
            ItemCreate(source_type="kobo", source_id="kobo-book-2", title="Other", content="x"),
        )
    pdf = _base_pdf()
    attach_source_pdf_and_enrich(item_id, pdf, "a.pdf", config=config)
    result = attach_source_pdf_and_enrich(other_id, pdf, "b.pdf", config=config)

    assert result.status == "enriched"
    assert len(_children(config, item_id)) == 3
    assert len(_children(config, other_id)) == 3

"""Tests for the persistence layer.

Drive an `EnrichedPdf` artifact directly into a real SQLite database and
verify the resulting rows. The extractor is not exercised here.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
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
    set_source_pdf,
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


def test_rerun_updates_child_in_place_and_drops_stale_images(config, parent_pdf_item):
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


def test_rerun_keeps_image_media_id_when_image_unchanged(config, parent_pdf_item):
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(images=[_image(0)]))
        first = conn.execute(
            "SELECT id FROM item_media WHERE item_id = ? AND source_origin = 'enricher'",
            (parent_pdf_item,),
        ).fetchall()
        persist_enrichment(
            conn, config, parent_pdf_item, _enriched(sha="b" * 64, images=[_image(0)])
        )
        second = conn.execute(
            "SELECT id FROM item_media WHERE item_id = ? AND source_origin = 'enricher'",
            (parent_pdf_item,),
        ).fetchall()
    assert [r["id"] for r in first] == [r["id"] for r in second]


def test_rerun_adopts_legacy_child_and_keeps_its_ocr(config, parent_pdf_item):
    """Children written before #196 used the bare /NM as source_id."""
    ann = _annotation(kind="margin_note", anchor="")
    with get_connection(config) as conn:
        legacy_id = conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, metadata, parent_id) "
            "VALUES ('pdf_annotation', ?, 'Margin note', 'transcribed words', ?, ?)",
            (
                ann.annotation_id,
                json.dumps(
                    {
                        "annotation_kind": "margin_note",
                        "annotation_id": ann.annotation_id,
                        "stroke_geometry_hash": ann.stroke_geometry_hash,
                        "ocr_text": "transcribed words",
                        "ocr_version": 1,
                    }
                ),
                parent_pdf_item,
            ),
        ).lastrowid
        conn.commit()

        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        rows = conn.execute(
            "SELECT id, source_id, content, metadata FROM items WHERE parent_id = ?",
            (parent_pdf_item,),
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == legacy_id
    assert row["source_id"] == f"item-{parent_pdf_item}:p{ann.page}:{ann.annotation_id}"
    assert row["content"] == "transcribed words"
    assert json.loads(row["metadata"])["ocr_version"] == 1


def test_annotations_without_ids_upsert_by_geometry_hash(config, parent_pdf_item):
    ann = _annotation()
    ann.annotation_id = None
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        (first,) = conn.execute(
            "SELECT id, source_id FROM items WHERE parent_id = ?", (parent_pdf_item,)
        ).fetchall()
        persist_enrichment(
            conn, config, parent_pdf_item, _enriched(sha="b" * 64, annotations=[ann])
        )
        (second,) = conn.execute(
            "SELECT id, source_id FROM items WHERE parent_id = ?", (parent_pdf_item,)
        ).fetchall()
    assert second["id"] == first["id"]
    assert second["source_id"] == f"item-{parent_pdf_item}:p{ann.page}:{ann.stroke_geometry_hash}"


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


def _child_ids(conn, parent_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, metadata FROM items WHERE parent_id = ? AND source_type = 'pdf_annotation'",
        (parent_id,),
    ).fetchall()
    return {json.loads(r["metadata"])["stroke_geometry_hash"]: r["id"] for r in rows}


def test_same_name_on_another_page_does_not_take_the_child(config, parent_pdf_item):
    """PyMuPDF restarts /NM on each page, so fitz-A0 can be on page 0 and page 3."""
    on_p3 = replace(_annotation(), page=3, annotation_id="fitz-A0", stroke_geometry_hash="3" * 64)
    on_p0 = replace(_annotation(), page=0, annotation_id="fitz-A0", stroke_geometry_hash="0" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[on_p3]))
        before = _child_ids(conn, parent_pdf_item)
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[on_p0, on_p3]))
        after = _child_ids(conn, parent_pdf_item)
    assert after["3" * 64] == before["3" * 64]
    assert len(after) == 2


def _child_rows(conn, parent_id: int) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT id, source_id, title, metadata FROM items "
        "WHERE parent_id = ? AND source_type = 'pdf_annotation'",
        (parent_id,),
    ).fetchall()
    return {r["id"]: {**dict(r), "meta": json.loads(r["metadata"])} for r in rows}


def test_reused_name_elsewhere_on_the_page_is_a_new_child(config, parent_pdf_item):
    """Erase fitz-A0 and draw elsewhere: PyMuPDF names the new stroke fitz-A0 too."""
    old = replace(_annotation(), annotation_id="fitz-A0", stroke_geometry_hash="1" * 64)
    new = replace(old, bbox=(400, 500, 450, 550), stroke_geometry_hash="2" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[old]))
        (old_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[new]))
        rows = _child_rows(conn, parent_pdf_item)
    assert len(rows) == 2
    assert rows[old_id]["meta"]["removed_from_pdf"]
    assert rows[old_id]["title"].startswith("[Removed from PDF] ")
    (new_id,) = set(rows) - {old_id}
    assert "removed_from_pdf" not in rows[new_id]["meta"]


def test_removed_child_comes_back_when_its_annotation_returns(config, parent_pdf_item):
    ann = _annotation()
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        (child_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[]))
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        rows = _child_rows(conn, parent_pdf_item)
    assert list(rows) == [child_id]
    assert "removed_from_pdf" not in rows[child_id]["meta"]
    assert not rows[child_id]["title"].startswith("[Removed")
    assert rows[child_id]["source_id"] == f"item-{parent_pdf_item}:p0:{ann.annotation_id}"


def test_merged_annotations_keep_the_absorbed_child(config, parent_pdf_item):
    """A stroke joining two annotations makes one; the other's child is kept, marked."""
    a = replace(_annotation(), annotation_id="fitz-A0", stroke_geometry_hash="a" * 64)
    b = replace(
        _annotation(),
        annotation_id="fitz-A3",
        bbox=(100, 240, 300, 260),
        stroke_geometry_hash="b" * 64,
    )
    merged = replace(a, bbox=(100, 200, 300, 260), stroke_geometry_hash="m" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[a, b]))
        ids = _child_ids(conn, parent_pdf_item)
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[merged]))
        rows = _child_rows(conn, parent_pdf_item)
    assert set(rows) == {ids["a" * 64], ids["b" * 64]}
    assert "removed_from_pdf" not in rows[ids["a" * 64]]["meta"]
    assert rows[ids["b" * 64]]["meta"]["removed_from_pdf"]


def test_stroke_added_to_unnamed_ink_keeps_the_child(config, parent_pdf_item):
    """Some writers leave /NM out, so the child is followed by page and place."""
    old = replace(_annotation(), annotation_id=None, stroke_geometry_hash="1" * 64)
    grown = replace(old, bbox=(90, 190, 320, 240), stroke_geometry_hash="2" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[old]))
        (old_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[grown]))
        rows = _child_rows(conn, parent_pdf_item)
    assert list(rows) == [old_id]
    assert "removed_from_pdf" not in rows[old_id]["meta"]


def test_new_file_without_url_drops_the_old_link(config, parent_pdf_item, tmp_path):
    with get_connection(config) as conn:
        add_item_media(
            conn,
            parent_pdf_item,
            media_type="source_pdf",
            url="https://drive.example/file",
            local_path="1/old.pdf",
        )
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/new.pdf")
        (url,) = conn.execute(
            "SELECT url FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (parent_pdf_item,),
        ).fetchone()
    assert url is None


def test_stroke_added_to_a_named_annotation_keeps_the_child(config, parent_pdf_item):
    old = replace(_annotation(), annotation_id="fitz-A0", stroke_geometry_hash="1" * 64)
    grown = replace(old, bbox=(90, 190, 320, 240), stroke_geometry_hash="2" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[old]))
        (old_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[grown]))
        (new_id,) = _child_ids(conn, parent_pdf_item).values()
    assert new_id == old_id


def test_unchanged_rerun_does_not_rewrite_children(config, parent_pdf_item):
    run = _enriched(annotations=[_annotation(), replace(_annotation(), page=2)])
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, run)
        children = list(_child_ids(conn, parent_pdf_item).values())
        marks = ",".join("?" * len(children))
        count = f"SELECT COUNT(*) FROM item_history WHERE item_id IN ({marks})"
        before = conn.execute(count, children).fetchone()[0]
        persist_enrichment(conn, config, parent_pdf_item, run)
        after = conn.execute(count, children).fetchone()[0]
    assert after == before


@pytest.mark.parametrize("old_path", [None, "1/gone.pdf"])
def test_missing_or_url_only_source_pdf_is_not_a_version(config, parent_pdf_item, old_path):
    """A Drive file downloaded again after its local copy vanished is not a new version."""
    with get_connection(config) as conn:
        add_item_media(
            conn,
            parent_pdf_item,
            media_type="source_pdf",
            url="https://drive.example/file",
            local_path=old_path,
        )
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/new.pdf")
        versions = conn.execute(
            "SELECT COUNT(*) FROM item_media "
            "WHERE item_id = ? AND media_type = 'source_pdf_version'",
            (parent_pdf_item,),
        ).fetchone()[0]
        current = conn.execute(
            "SELECT local_path FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (parent_pdf_item,),
        ).fetchall()
    assert versions == 0
    assert [r["local_path"] for r in current] == ["1/new.pdf"]


def test_erasing_the_first_stroke_keeps_the_child(config, parent_pdf_item):
    """The group is named after its first stroke; erasing it renames the group."""
    note = replace(
        _annotation(kind="margin_note"),
        annotation_id="fitz-A0",
        member_ids=("fitz-A0", "fitz-A1", "fitz-A2"),
        stroke_geometry_hash="1" * 64,
    )
    shrunk = replace(
        note,
        annotation_id="fitz-A1",
        member_ids=("fitz-A1", "fitz-A2"),
        stroke_geometry_hash="2" * 64,
    )
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[note]))
        (child_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[shrunk]))
        rows = _child_rows(conn, parent_pdf_item)
    assert list(rows) == [child_id]
    assert "removed_from_pdf" not in rows[child_id]["meta"]


def test_removed_child_does_not_take_a_live_childs_annotation(config, parent_pdf_item):
    """A erased; B drawn nearby reuses the name fitz-A0; then B grows."""
    a = replace(_annotation(), annotation_id="fitz-A0", stroke_geometry_hash="a" * 64)
    # B starts clear of A's box, then grows to overlap both
    b = replace(a, bbox=(100, 230, 300, 250), stroke_geometry_hash="b" * 64)
    b_grown = replace(b, bbox=(100, 215, 300, 250), stroke_geometry_hash="c" * 64)
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[a]))
        a_id = _child_ids(conn, parent_pdf_item)["a" * 64]
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[]))
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[b]))
        b_id = _child_ids(conn, parent_pdf_item)["b" * 64]
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[b_grown]))
        rows = _child_rows(conn, parent_pdf_item)
    assert b_id != a_id
    assert rows[b_id]["meta"]["stroke_geometry_hash"] == "c" * 64
    assert "removed_from_pdf" not in rows[b_id]["meta"]
    assert rows[a_id]["meta"]["removed_from_pdf"]


def test_removal_is_logged_as_the_enrichers_change(config, parent_pdf_item):
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[_annotation()]))
        (child_id,) = _child_ids(conn, parent_pdf_item).values()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[]))
        reasons = {
            r[0]
            for r in conn.execute(
                "SELECT change_reason FROM item_history WHERE item_id = ?", (child_id,)
            )
        }
    assert reasons == {"enricher"}


def test_detached_former_child_does_not_block_a_new_one(config, parent_pdf_item):
    """A child moved to another parent by hand keeps its key; a new child must not clash."""
    ann = _annotation()
    with get_connection(config) as conn:
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        (child_id,) = _child_ids(conn, parent_pdf_item).values()
        conn.execute("UPDATE items SET parent_id = NULL WHERE id = ?", (child_id,))
        conn.commit()
        persist_enrichment(conn, config, parent_pdf_item, _enriched(annotations=[ann]))
        rows = _child_rows(conn, parent_pdf_item)
    assert len(rows) == 1
    assert child_id not in rows


def test_same_file_in_a_folded_legacy_row_keeps_its_link(config, parent_pdf_item):
    with get_connection(config) as conn:
        add_item_media(conn, parent_pdf_item, media_type="source_pdf", local_path="1/old.pdf")
        add_item_media(
            conn,
            parent_pdf_item,
            media_type="source_pdf",
            url="https://drive.example/file",
            local_path="1/new.pdf",
        )
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/new.pdf")
        rows = conn.execute(
            "SELECT local_path, url FROM item_media "
            "WHERE item_id = ? AND media_type = 'source_pdf'",
            (parent_pdf_item,),
        ).fetchall()
    assert [tuple(r) for r in rows] == [("1/new.pdf", "https://drive.example/file")]


def test_a_stored_source_pdf_replaced_by_another_file(config, parent_pdf_item):
    """The row's stored file is the old one: it moves to the version row, and the
    current row waits for the next adopt rather than naming the old file (#217)."""
    from lestash.core.blobs import adopt_media, media_file, prune_media
    from lestash.core.database import get_media_dir

    media = get_media_dir(config)
    (media / "1").mkdir(parents=True, exist_ok=True)
    (media / "1/old.pdf").write_bytes(b"%PDF old")
    with get_connection(config) as conn:
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/old.pdf")
        conn.commit()
        adopt_media(conn, config)
        prune_media(conn, config)  # the old file is now only in the store
        (media / "1").mkdir(parents=True, exist_ok=True)
        (media / "1/new.pdf").write_bytes(b"%PDF new")
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/new.pdf")
        rows = {
            r["media_type"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM item_media WHERE item_id = ?", (parent_pdf_item,)
            ).fetchall()
        }
        assert media_file(rows["source_pdf"], config).read_bytes() == b"%PDF new"
        assert media_file(rows["source_pdf_version"], config).read_bytes() == b"%PDF old"
        conn.commit()
        adopt_media(conn, config)
        prune_media(conn, config)
        current = dict(
            conn.execute(
                "SELECT * FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
                (parent_pdf_item,),
            ).fetchone()
        )
    assert current["sha256"] and not (media / "1/new.pdf").exists()
    assert media_file(current, config).read_bytes() == b"%PDF new"


def test_enrichment_and_ocr_read_a_pruned_source_pdf_from_the_store(config, parent_pdf_item):
    from lestash.core.blobs import adopt_media, prune_media
    from lestash.core.database import get_media_dir
    from lestash.core.pdf_enrich.ocr import _resolve_parent_pdf
    from lestash.core.pdf_enrich.runner import _resolve_source_pdf

    media = get_media_dir(config)
    (media / "1").mkdir(parents=True, exist_ok=True)
    (media / "1/doc.pdf").write_bytes(b"%PDF doc")
    with get_connection(config) as conn:
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/doc.pdf")
        conn.commit()
        adopt_media(conn, config)
        assert prune_media(conn, config).removed == 1
        item_row = (parent_pdf_item, None, None, None)  # no drive_id: no download
        assert _resolve_source_pdf(conn, config, item_row).read_bytes() == b"%PDF doc"
        assert _resolve_parent_pdf(conn, config, parent_pdf_item).read_bytes() == b"%PDF doc"


def test_reattaching_the_same_stored_source_pdf_keeps_its_file(config, parent_pdf_item):
    from lestash.core.blobs import adopt_media
    from lestash.core.database import get_media_dir

    media = get_media_dir(config)
    (media / "1").mkdir(parents=True, exist_ok=True)
    (media / "1/doc.pdf").write_bytes(b"%PDF doc")
    with get_connection(config) as conn:
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/doc.pdf")
        conn.commit()
        adopt_media(conn, config)
        set_source_pdf(conn, config, parent_pdf_item, local_path="1/doc.pdf")
        (sha,) = conn.execute(
            "SELECT sha256 FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (parent_pdf_item,),
        ).fetchone()
    assert sha

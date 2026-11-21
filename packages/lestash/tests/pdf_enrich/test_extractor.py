"""End-to-end tests for `enrich_pdf` against synthesised PDFs.

These exercise the real PyMuPDF surface (links, images, ink annotations) with
Docling stubbed out — Docling is large, slow, and tested separately in
`test_text_extract.py`. We replace `convert_pdf` so each test can
control the markdown the enricher post-processes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _stub_docling(markdown: str, regions=None):
    return patch(
        "lestash.core.pdf_enrich.extractor.convert_pdf",
        return_value=(markdown, regions or []),
    )


def test_enrich_pdf_returns_artifact_with_sha_and_version(make_pdf):
    pdf: Path = make_pdf([{"text": [((50, 50, 500, 100), "hello world")]}])
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("hello world\n"):
        result = enrich_pdf(pdf)
    assert result.content == "hello world\n"
    assert len(result.pdf_sha256) == 64
    assert result.extractor_version >= 1


def test_enrich_pdf_extracts_link_and_rewrites_anchor(make_pdf):
    pdf = make_pdf(
        [
            {
                "text": [((50, 50, 500, 80), "see the docs")],
                "links": [((50, 90, 500, 110), "https://docs.example", "the docs")],
            }
        ]
    )
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("see the docs page\n"):
        result = enrich_pdf(pdf)
    assert "(https://docs.example)" in result.content


def test_enrich_pdf_extracts_image_bytes(make_pdf, red_dot_png):
    pdf = make_pdf(
        [
            {
                "text": [((50, 50, 500, 80), "report")],
                "images": [((100, 200, 200, 300), red_dot_png)],
            }
        ]
    )
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("report\n\n<!-- image -->\n"):
        result = enrich_pdf(pdf)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.bytes_  # non-empty bytes
    assert img.mime_type.startswith("image/")
    assert len(img.xref_hash) == 64


def test_enrich_pdf_emits_one_entry_per_image_occurrence(make_pdf, red_dot_png):
    """Same PNG used twice should produce two ExtractedImage entries (so each
    Docling placeholder gets a replacement) but they share an xref_hash —
    persistence dedups by hash to a single media row. See #143.
    """
    pdf = make_pdf(
        [
            {
                "images": [
                    ((100, 200, 150, 250), red_dot_png),
                    ((300, 200, 350, 250), red_dot_png),
                ]
            }
        ]
    )
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("<!-- image -->\n<!-- image -->\n"):
        result = enrich_pdf(pdf)
    assert len(result.images) == 2
    assert result.images[0].xref_hash == result.images[1].xref_hash


def test_enrich_pdf_extracts_ink_annotation(make_pdf):
    horizontal_underline = [(100, 500), (160, 500), (220, 500), (280, 500)]
    pdf = make_pdf(
        [
            {
                "text": [((50, 480, 500, 510), "underlined phrase here")],
                "ink": [horizontal_underline],
            }
        ]
    )
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("underlined phrase here\n"):
        result = enrich_pdf(pdf)
    assert len(result.annotations) >= 1
    # Long thin horizontal stroke → underline
    assert result.annotations[0].kind == "underline"


def test_enrich_pdf_strips_trailing_bullet_artifacts(make_pdf):
    pdf = make_pdf([{"text": [((50, 50, 500, 100), "x")]}])
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("- first ·\n- second ◦\n"):
        result = enrich_pdf(pdf)
    assert "·" not in result.content
    assert "◦" not in result.content


def test_enrich_pdf_handles_corrupt_input_gracefully(tmp_path: Path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    from lestash.core.pdf_enrich import enrich_pdf

    with _stub_docling("fallback content\n"):
        result = enrich_pdf(bad)
    # Even when PyMuPDF can't open the file, we still return a result with
    # Docling's content and a sha256 — never raise.
    assert "fallback content" in result.content
    assert result.images == []
    assert result.annotations == []


def test_enrich_pdf_saves_a_diagram_drawn_as_shapes(tmp_path):
    """#391: Docling's picture region fills the placeholder when no raster image exists."""
    import pymupdf
    from lestash.core.pdf_enrich import enrich_pdf
    from lestash.core.text_extract import PictureRegion

    doc = pymupdf.open()
    doc.new_page(width=612, height=792).draw_rect(pymupdf.Rect(100, 100, 300, 200), fill=(0, 0, 1))
    pdf = tmp_path / "diagram.pdf"
    doc.save(pdf)
    doc.close()

    region = PictureRegion(0, (90, 90, 310, 210))
    with _stub_docling("before\n\n<!-- image -->\n\nafter\n", [region]):
        result = enrich_pdf(pdf)

    assert [(img.placeholder_index, img.mime_type) for img in result.images] == [(0, "image/png")]


def test_enrich_pdf_falls_back_to_raster_order_when_regions_do_not_match(make_pdf, red_dot_png):
    from lestash.core.pdf_enrich import enrich_pdf
    from lestash.core.text_extract import PictureRegion

    pdf = make_pdf([{"images": [((100, 100, 200, 200), red_dot_png)]}])
    two_regions = [PictureRegion(0, (0, 0, 10, 10)), PictureRegion(0, (20, 20, 30, 30))]
    with _stub_docling("<!-- image -->\n", two_regions):
        result = enrich_pdf(pdf)

    assert [img.placeholder_index for img in result.images] == [0]
    assert result.images[0].bbox == (100.0, 100.0, 200.0, 200.0)

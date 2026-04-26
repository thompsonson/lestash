"""End-to-end tests for `enrich_pdf` against synthesised PDFs.

These exercise the real PyMuPDF surface (links, images, ink annotations) with
Docling stubbed out — Docling is large, slow, and tested separately in
`test_text_extract.py`. We replace `convert_to_markdown` so each test can
control the markdown the enricher post-processes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _stub_docling(markdown: str):
    return patch(
        "lestash.core.pdf_enrich.extractor.convert_to_markdown",
        return_value=markdown,
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


def test_enrich_pdf_dedups_repeated_image_xref(make_pdf, red_dot_png):
    # Same PNG inserted twice — should appear once in the output
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
    assert len(result.images) == 1


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

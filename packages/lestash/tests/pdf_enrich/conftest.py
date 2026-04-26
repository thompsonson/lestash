"""Test fixtures for the PDF enrichment pipeline.

Uses PyMuPDF to synthesise small PDFs at test time so we don't need to commit
binary fixture files. Each builder produces a minimal PDF that exercises one
extractor (links / images / ink annotations).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def make_pdf(tmp_path: Path):
    """Factory: returns a function `build(pages)` that produces a PDF file.

    Each `page` is a dict with optional keys:
        text:   list of (rect, content) — drawn text spans
        links:  list of (rect, uri, anchor) — URI link annotations + their text
        images: list of (rect, png_bytes) — embedded raster images
        ink:    list of stroke-lists, each stroke a list of (x, y) points
    """
    import pymupdf

    def build(pages, name: str = "test.pdf") -> Path:
        doc = pymupdf.open()
        for page_spec in pages:
            page = doc.new_page(width=612, height=792)  # US Letter

            for rect, content in page_spec.get("text", []):
                r = pymupdf.Rect(*rect)
                page.insert_textbox(r, content, fontsize=11)

            for rect, uri, anchor in page_spec.get("links", []):
                r = pymupdf.Rect(*rect)
                # Render the anchor text first so PyMuPDF has something to clip.
                page.insert_textbox(r, anchor, fontsize=11, color=(0, 0, 1))
                page.insert_link({"kind": pymupdf.LINK_URI, "from": r, "uri": uri})

            for rect, png_bytes in page_spec.get("images", []):
                r = pymupdf.Rect(*rect)
                page.insert_image(r, stream=png_bytes)

            for stroke_list in page_spec.get("ink", []):
                annot = page.add_ink_annot([stroke_list])
                annot.set_border(width=1.0)
                annot.update()

        out = tmp_path / name
        doc.save(out)
        doc.close()
        return out

    return build


def _solid_png(rgb: tuple[int, int, int]) -> bytes:
    """Build a tiny solid-colour PNG using PyMuPDF's pixmap API."""
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


@pytest.fixture
def red_dot_png() -> bytes:
    return _solid_png((255, 0, 0))


@pytest.fixture
def blue_dot_png() -> bytes:
    return _solid_png((0, 0, 255))

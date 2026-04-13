"""Tests for text_extract (Docling-backed document conversion)."""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from lestash.core.text_extract import convert_to_markdown, extract_content


def _install_fake_docling(markdown: str) -> MagicMock:
    """Install a fake `docling.document_converter` module so `convert_to_markdown`
    can import it without the real (heavy) dependency being installed.

    Returns the converter instance mock for assertions.
    """
    mock_converter_instance = MagicMock()
    mock_result = MagicMock()
    mock_result.document.export_to_markdown.return_value = markdown
    mock_converter_instance.convert.return_value = mock_result

    mock_class = MagicMock(return_value=mock_converter_instance)

    fake_module = types.ModuleType("docling.document_converter")
    fake_module.DocumentConverter = mock_class  # type: ignore[attr-defined]
    fake_parent = types.ModuleType("docling")

    sys.modules["docling"] = fake_parent
    sys.modules["docling.document_converter"] = fake_module
    return mock_converter_instance


class TestConvertToMarkdown:
    def test_returns_markdown_from_docling(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

        converter = _install_fake_docling("# Title\n\nhello")
        try:
            md = convert_to_markdown(pdf)
        finally:
            sys.modules.pop("docling.document_converter", None)
            sys.modules.pop("docling", None)

        assert md == "# Title\n\nhello"
        converter.convert.assert_called_once_with(str(pdf))

    def test_returns_empty_on_docling_failure(self, tmp_path):
        pdf = tmp_path / "broken.pdf"
        pdf.write_bytes(b"not really a pdf")

        fake_module = types.ModuleType("docling.document_converter")

        class _Boom:
            def __init__(self):
                raise RuntimeError("docling exploded")

        fake_module.DocumentConverter = _Boom  # type: ignore[attr-defined]
        sys.modules["docling"] = types.ModuleType("docling")
        sys.modules["docling.document_converter"] = fake_module
        try:
            md = convert_to_markdown(pdf)
        finally:
            sys.modules.pop("docling.document_converter", None)
            sys.modules.pop("docling", None)

        assert md == ""


class TestExtractContent:
    def test_pdf_mime_dispatches_to_docling(self, tmp_path):
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"x")
        with patch(
            "lestash.core.text_extract.convert_to_markdown",
            return_value="# md",
        ) as mock_conv:
            out = extract_content(pdf, "application/pdf")
        assert out == "# md"
        mock_conv.assert_called_once_with(pdf)

    def test_docx_mime_dispatches_to_docling(self, tmp_path):
        path = tmp_path / "a.docx"
        path.write_bytes(b"x")
        docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        with patch(
            "lestash.core.text_extract.convert_to_markdown",
            return_value="ok",
        ) as mock_conv:
            out = extract_content(path, docx_mime)
        assert out == "ok"
        mock_conv.assert_called_once()

    def test_plain_text_is_read_directly(self, tmp_path):
        path = tmp_path / "note.txt"
        path.write_text("hello world", encoding="utf-8")
        assert extract_content(path, "text/plain") == "hello world"

    def test_markdown_is_read_directly(self, tmp_path):
        path = tmp_path / "note.md"
        path.write_text("# h", encoding="utf-8")
        assert extract_content(path, "text/markdown") == "# h"

    def test_csv_is_read_directly(self, tmp_path):
        path = tmp_path / "rows.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")
        assert extract_content(path, "text/csv") == "a,b\n1,2\n"

    def test_unsupported_mime_returns_empty(self, tmp_path):
        path = tmp_path / "img.png"
        path.write_bytes(b"\x89PNG")
        assert extract_content(path, "image/png") == ""

    def test_text_read_failure_returns_empty(self, tmp_path):
        path = tmp_path / "missing.txt"
        # File does not exist → read_text raises → caught → ""
        assert extract_content(Path(path), "text/plain") == ""

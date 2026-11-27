"""Pictures from Word files and Google Docs fill their markers (#394)."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pytest
from lestash.core import doc_pictures
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, get_media_dir, init_database, upsert_item
from lestash.core.doc_pictures import PICTURE_ORIGIN, attach_pictures
from lestash.core.text_extract import DocPicture, convert_docx
from lestash.models.item import ItemCreate
from PIL import Image


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (200, 10, 10)).save(buffer, "JPEG")
    return buffer.getvalue()


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 255, 128)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def config():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(general=GeneralConfig(database_path=str(Path(tmp) / "t.db")))
        init_database(cfg)
        yield cfg


def _item(config: Config, content: str) -> int:
    with get_connection(config) as conn:
        item_id = upsert_item(
            conn,
            ItemCreate(source_type="google-drive", source_id="doc-1", title="Doc", content=content),
        )
        conn.commit()
    return item_id


def _content(config: Config, item_id: int) -> str:
    with get_connection(config) as conn:
        return conn.execute("SELECT content FROM items WHERE id = ?", (item_id,)).fetchone()[0]


def _rows(config: Config, item_id: int) -> list[tuple]:
    with get_connection(config) as conn:
        return conn.execute(
            "SELECT id, mime_type, position, local_path FROM item_media "
            "WHERE item_id = ? AND source_origin = ? ORDER BY position",
            (item_id, PICTURE_ORIGIN),
        ).fetchall()


class TestConvertDocx:
    def test_each_marker_gets_its_picture_and_a_photo_stays_a_jpeg(self, tmp_path):
        import docx

        jpeg, png = _jpeg(), _png()
        document = docx.Document()
        document.add_paragraph("before")
        document.add_picture(io.BytesIO(jpeg))
        document.add_paragraph("middle")
        document.add_picture(io.BytesIO(png))
        path = tmp_path / "t.docx"
        document.save(str(path))

        markdown, pictures = convert_docx(path)

        assert markdown.count("<!-- image -->") == 2
        assert pictures == [DocPicture(jpeg, "image/jpeg"), DocPicture(png, "image/png")]

    def test_a_file_docling_cannot_read_gives_no_pictures(self, tmp_path):
        path = tmp_path / "broken.docx"
        path.write_bytes(b"not a word file")
        assert convert_docx(path) == ("", None)

    def test_an_unreadable_file_in_the_word_file_keeps_the_others(self, tmp_path):
        import zipfile

        jpeg = _jpeg()
        path = tmp_path / "t.docx"
        path.write_bytes(_word_file(jpeg))
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("word/media/odd.png", b"\x89PNG not really")

        _markdown, pictures = convert_docx(path)

        assert pictures == [DocPicture(jpeg, "image/jpeg")]


class TestAttachPictures:
    def test_markers_link_to_saved_files_in_order(self, config):
        item_id = _item(config, "a <!-- image --> b <!-- image --> c <!-- image -->")
        jpeg, png = _jpeg(), _png()

        count = attach_pictures(
            item_id,
            [DocPicture(jpeg, "image/jpeg"), None, DocPicture(png, "image/png")],
            config=config,
        )

        rows = _rows(config, item_id)
        assert count == 2
        assert [(r[1], r[2]) for r in rows] == [("image/jpeg", 0), ("image/png", 2)]
        assert (get_media_dir(config) / rows[0][3]).read_bytes() == jpeg
        assert _content(config, item_id) == (
            f"a ![Picture 1](/api/media/{rows[0][0]}) b <!-- image --> "
            f"c ![Picture 3](/api/media/{rows[1][0]})"
        )

    def test_the_same_picture_twice_is_one_file(self, config):
        item_id = _item(config, "<!-- image --> <!-- image -->")
        jpeg = DocPicture(_jpeg(), "image/jpeg")

        attach_pictures(item_id, [jpeg, jpeg], config=config)

        (row,) = _rows(config, item_id)
        assert _content(config, item_id) == (
            f"![Picture 1](/api/media/{row[0]}) ![Picture 2](/api/media/{row[0]})"
        )

    def test_a_rerun_keeps_rows_and_drops_pictures_now_gone(self, config):
        item_id = _item(config, "<!-- image --> <!-- image -->")
        jpeg, png = DocPicture(_jpeg(), "image/jpeg"), DocPicture(_png(), "image/png")
        attach_pictures(item_id, [jpeg, png], config=config)
        first = _rows(config, item_id)

        # A re-sync writes the markers back, and the document has lost its PNG
        with get_connection(config) as conn:
            conn.execute("UPDATE items SET content = ? WHERE id = ?", ("<!-- image -->", item_id))
            conn.commit()
        attach_pictures(item_id, [jpeg], config=config)

        assert _rows(config, item_id) == first[:1]
        media = get_media_dir(config)
        assert (media / first[0][3]).exists()
        assert not (media / first[1][3]).exists()

    def test_a_document_now_without_pictures_loses_their_rows(self, config):
        item_id = _item(config, "<!-- image -->")
        attach_pictures(item_id, [DocPicture(_jpeg(), "image/jpeg")], config=config)
        (row,) = _rows(config, item_id)
        with get_connection(config) as conn:
            conn.execute("UPDATE items SET content = 'no pictures now' WHERE id = ?", (item_id,))
            conn.commit()

        assert attach_pictures(item_id, [], config=config) == 0

        assert _rows(config, item_id) == []
        assert not (get_media_dir(config) / row[3]).exists()
        assert _content(config, item_id) == "no pictures now"

    def test_content_with_other_markers_is_left_alone(self, config):
        item_id = _item(config, "edited <!-- image -->")

        count = attach_pictures(item_id, [DocPicture(_jpeg(), "image/jpeg")] * 2, config=config)

        assert count == 0
        assert _content(config, item_id) == "edited <!-- image -->"
        assert _rows(config, item_id) == []
        assert not (get_media_dir(config) / str(item_id)).exists() or not any(
            (get_media_dir(config) / str(item_id)).iterdir()
        )

    def test_files_are_saved_with_no_write_open(self, config, monkeypatch):
        item_id = _item(config, "<!-- image -->")
        real_save = doc_pictures.save_media_file

        def save_and_check(*args, **kwargs):
            with get_connection(config) as conn:
                conn.execute("BEGIN IMMEDIATE")  # fails if another write is open
                conn.rollback()
            return real_save(*args, **kwargs)

        monkeypatch.setattr(doc_pictures, "save_media_file", save_and_check)
        assert attach_pictures(item_id, [DocPicture(_jpeg(), "image/jpeg")], config=config) == 1


def _word_file(*pictures: bytes) -> bytes:
    import docx

    document = docx.Document()
    for data in pictures:
        document.add_paragraph("text")
        document.add_picture(io.BytesIO(data))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _Request:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def execute(self) -> bytes:
        return self.data


class _Files:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def export(self, fileId: str, mimeType: str) -> _Request:  # noqa: N803 (Drive's names)
        return _Request(self.data)

    def get(self, fileId: str, fields: str) -> _Request:  # noqa: N803
        return _Request(
            {"id": fileId, "name": "Doc", "mimeType": "application/vnd.google-apps.document"}
        )


class _Service:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def files(self) -> _Files:
        return _Files(self.data)


def test_a_google_doc_brings_its_pictures(tmp_path, monkeypatch):
    from lestash.core import google_drive

    jpeg = _jpeg()
    monkeypatch.setattr(google_drive, "_cache_dir", lambda: tmp_path)
    meta = {"id": "G1", "name": "Doc", "mimeType": "application/vnd.google-apps.document"}

    drive_file = google_drive._process_file(_Service(_word_file(jpeg)), meta)

    assert drive_file.item.content.count("<!-- image -->") == 1
    assert drive_file.pictures == [DocPicture(jpeg, "image/jpeg")]
    assert drive_file.pdf_bytes is None


def test_the_pictures_command_fills_held_documents(config, monkeypatch, tmp_path):
    from lestash.cli.main import app
    from lestash.core import google_auth, google_drive
    from typer.testing import CliRunner

    jpeg = _jpeg()
    with get_connection(config) as conn:
        held = upsert_item(
            conn,
            ItemCreate(
                source_type="google-drive",
                source_id="G1",
                title="Doc",
                content="old <!-- image -->",
                metadata={
                    "mime_type": "application/vnd.google-apps.document",
                    "folder_path": "Course/Week 1",
                    "categories": ["Course", "Week 1"],
                },
            ),
        )
        conn.commit()
    monkeypatch.setattr("lestash.core.config.Config.load", lambda: config)
    monkeypatch.setattr(google_drive, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(google_auth, "get_drive_service", lambda: _Service(_word_file(jpeg)))

    result = CliRunner().invoke(app, ["google", "pictures"])

    assert result.exit_code == 0, result.output
    assert "Saved 1 pictures in 1 documents" in result.output
    (row,) = _rows(config, held)
    assert (get_media_dir(config) / row[3]).read_bytes() == jpeg
    assert f"![Picture 1](/api/media/{row[0]})" in _content(config, held)
    with get_connection(config) as conn:
        (metadata,) = conn.execute("SELECT metadata FROM items WHERE id = ?", (held,)).fetchone()
    assert '"folder_path": "Course/Week 1"' in metadata

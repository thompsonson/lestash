"""Reading a snapshot into items, and storing them through SyncStore."""

import json
from datetime import UTC, datetime
from pathlib import Path

from lestash.core.database import get_connection, get_media_dir
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo.reader import KoboReader, device_path, parse_date
from lestash_kobo.store import KoboStore

from .conftest import DRIVE_PDF, NN, NOTEBOOK, STORE_UNOPENED

T1 = datetime(2026, 11, 7, 9, 56, tzinfo=UTC)


def _items(device: Path, tmp_path: Path, **kw):
    store = KoboStore(tmp_path / "store")
    snap = store.take(device, T1).snapshot
    return list(KoboReader(store, snap, media_dir=tmp_path / "media", **kw).items())


def test_titles_skip_notebooks_and_unopened_store_books(device: Path, tmp_path: Path) -> None:
    ids = {i.source_id for i in _items(device, tmp_path) if not i.source_id.startswith("bookmark:")}
    assert ids == {NN, DRIVE_PDF}
    assert NOTEBOOK not in ids and STORE_UNOPENED not in ids


def test_store_titles_can_be_included(device: Path, tmp_path: Path) -> None:
    ids = {i.source_id for i in _items(device, tmp_path, include_store_titles=True)}
    assert STORE_UNOPENED in ids and NOTEBOOK not in ids


def test_book_item_fields(device: Path, tmp_path: Path) -> None:
    book = next(i for i in _items(device, tmp_path) if i.source_id == NN)
    assert book.source_type == "kobo"
    assert book.title == "Neural Networks for Beginners"
    assert book.author == "Brian Murray"
    assert book.content == "An easy & gentle intro."
    assert book.metadata["percent_read"] == 40
    assert book.metadata["publisher"] == "May Reads"  # zero-width space stripped
    assert book.metadata["kind"] == "store"


def test_drive_pdf_records_its_file_in_the_snapshot(device: Path, tmp_path: Path) -> None:
    pdf = next(i for i in _items(device, tmp_path) if i.source_id == DRIVE_PDF)
    assert pdf.metadata["kind"] == "drive_pdf"
    assert pdf.metadata["device_path"] == ".kobo/google_drive/Papers/Judge.pdf"
    assert len(pdf.metadata["file_sha256"]) == 64


def test_bookmarks_in_reading_order_without_hidden(device: Path, tmp_path: Path) -> None:
    marks = [i for i in _items(device, tmp_path) if i.source_id.startswith("bookmark:")]
    nn = [m.source_id for m in marks if m.metadata["_parent_source_id"] == NN]
    assert nn == ["bookmark:hl-1", "bookmark:mk-1", "bookmark:hl-2"]
    assert "bookmark:hidden-1" not in {m.source_id for m in marks}
    first = marks[0]
    assert first.content == "first\n\nmy note"
    assert first.title == "Highlight: first"
    assert first.created_at == datetime(2024, 10, 3, 14, 51, 19, tzinfo=UTC)


def test_markup_carries_its_images(device: Path, tmp_path: Path) -> None:
    markup = next(i for i in _items(device, tmp_path) if i.source_id == "bookmark:mk-1")
    assert markup.content == "[Handwritten markup]"
    assert [m.mime_type for m in markup.media or []] == ["image/jpeg", "image/svg+xml"]
    jpg = (tmp_path / "media" / markup.media[0].local_path).read_bytes()
    assert jpg == b"\xff\xd8jpeg"


def test_sync_stores_children_under_books_and_is_idempotent(
    device: Path, tmp_path: Path, test_db
) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    plugin = KoboSource()
    config = {"store_dir": str(store.root)}
    for _ in range(2):
        with get_connection(test_db) as conn:
            SyncStore(conn, "kobo").run(plugin.sync(config))
    with get_connection(test_db) as conn:
        rows = conn.execute(
            "SELECT i.source_id, p.source_id FROM items i LEFT JOIN items p ON p.id = i.parent_id"
            " WHERE i.source_type = 'kobo' ORDER BY i.source_id"
        ).fetchall()
        media = conn.execute("SELECT local_path FROM item_media").fetchall()
    assert dict(rows) == {
        DRIVE_PDF: None,
        NN: None,
        "bookmark:dg-1": DRIVE_PDF,
        "bookmark:hl-1": NN,
        "bookmark:hl-2": NN,
        "bookmark:mk-1": NN,
    }
    assert len(media) == 2  # the second sync didn't add the images again
    assert all((get_media_dir(test_db) / m[0]).is_file() for m in media)


def test_sync_without_snapshot_warns(tmp_path: Path, caplog) -> None:
    items = list(KoboSource().sync({"store_dir": str(tmp_path / "empty")}))
    assert items == []
    assert "No Kobo snapshot" in caplog.text


def test_helpers() -> None:
    assert device_path("file:///mnt/onboard/My%20Notebooks/a.nebo") == "My Notebooks/a.nebo"
    assert device_path("a1b2") is None
    assert parse_date("2024-10-03T14:51:19.000") == datetime(2024, 10, 3, 14, 51, 19, tzinfo=UTC)
    assert parse_date("2026-11-07T07:08:17Z") == datetime(2026, 11, 7, 7, 8, 17, tzinfo=UTC)
    assert parse_date("") is None and parse_date("garbage") is None


def test_metadata_is_json_serialisable(device: Path, tmp_path: Path) -> None:
    for item in _items(device, tmp_path):
        json.dumps(item.metadata)

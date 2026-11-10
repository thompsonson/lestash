"""Text from NotebookLM slides, infographics and apps (#218, W3)."""

from __future__ import annotations

import io
import json

import pytest
from lestash.core import extract
from lestash.core.blobs import BlobStore, store_root
from lestash.core.database import get_connection
from lestash.core.extract import PART, Media, base_content, compose, extract_pending, page_text
from lestash.core.sync import SyncStore
from lestash.models.item import ItemCreate, MediaCreate

NB = "Takeout/NotebookLM/Hebbian"


@pytest.fixture
def fake_ocr(monkeypatch):
    """OCR that 'reads' a picture's bytes as its text, and counts its reads."""
    reads: list[str] = []

    def read(cls, path):
        reads.append(path.name)
        return path.read_bytes().decode()

    monkeypatch.setattr(extract._Ocr, "read", classmethod(read))
    monkeypatch.setattr(extract._Ocr, "version", "fake-ocr 1")
    return reads


def _deck(store: BlobStore, slides: list[bytes], app: bytes | None = None) -> ItemCreate:
    media = []
    for n, data in enumerate(slides, start=1):
        blob = store.put_stream(io.BytesIO(data))
        media.append(
            MediaCreate(
                media_type="image",
                mime_type="image/png",
                alt_text=f"Slide {n}",
                position=n,
                source_origin="takeout",
                sha256=blob.sha256,
                size=blob.size,
            )
        )
    if app is not None:
        blob = store.put_stream(io.BytesIO(app))
        media.append(
            MediaCreate(
                media_type="file",
                mime_type="text/html",
                alt_text="Quiz.html",
                position=0,
                source_origin="takeout",
                sha256=blob.sha256,
                size=blob.size,
            )
        )
    return ItemCreate(
        source_type="notebooklm",
        source_id=f"{NB}/Artifacts/Deck metadata",
        title="Deck",
        content="# Deck\n\nNotebookLM slides",
        is_own_content=True,
        metadata={"type": "artifact", "artifact_type": "slides", "_parent_source_id": NB},
        media=media,
    )


def _item(conn) -> tuple[int, str, dict]:
    row = conn.execute(
        "SELECT id, content, metadata FROM items WHERE source_id = ?",
        (f"{NB}/Artifacts/Deck metadata",),
    ).fetchone()
    return row[0], row[1], json.loads(row[2])


def test_slides_get_their_text_under_slide_headings(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"Hebbian learning", b"Fire together"])])
        counts = extract_pending(conn, test_db)
        _, content, metadata = _item(conn)
    assert (counts.files, counts.items, counts.failed) == (2, 1, 0)
    assert content == (
        f"# Deck\n\nNotebookLM slides\n\n{PART}\n\n"
        "## Slide 1\n\nHebbian learning\n\n## Slide 2\n\nFire together"
    )
    assert metadata["text_from"] == ["fake-ocr 1"]


def test_a_file_is_read_once_and_an_import_keeps_the_text(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"one", b"one"])])  # same picture
        extract_pending(conn, test_db)
        assert len(fake_ocr) == 1
        _, before, _ = _item(conn)
        history = conn.execute("SELECT count(*) FROM item_history").fetchone()[0]
        # The next export yields the item with its description's content only
        again = SyncStore(conn, "notebooklm")
        again.run([_deck(store, [b"one", b"one"])])
        assert again.counts.updated == 0 and again.counts.unchanged == 1
        assert _item(conn)[1] == before
        assert conn.execute("SELECT count(*) FROM item_history").fetchone()[0] == history
        assert extract_pending(conn, test_db).files == 0 and len(fake_ocr) == 1


def test_a_slide_from_a_later_part_adds_its_text(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"first"])])
        extract_pending(conn, test_db)
        later = _deck(store, [b"first", b"second"])
        later.media = later.media[1:]  # the later part holds slide two only
        SyncStore(conn, "notebooklm").run([later])
        extract_pending(conn, test_db)
        _, content, _ = _item(conn)
    assert content.endswith("## Slide 1\n\nfirst\n\n## Slide 2\n\nsecond")


def test_an_app_page_gives_its_visible_text_and_never_its_scripts(test_db, fake_ocr) -> None:
    page = (
        b"<html><head><style>p{}</style><script>alert('x')</script></head>"
        b"<body><h1>Quiz</h1><p>What  fires\ntogether?</p></body></html>"
    )
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [], app=page)])
        extract_pending(conn, test_db)
        _, content, metadata = _item(conn)
    assert content.endswith(f"{PART}\n\nQuiz\nWhat fires together?")
    assert metadata["text_from"] == ["html.parser"]


def test_a_file_that_cannot_be_read_is_recorded_and_not_read_again(
    test_db, fake_ocr, monkeypatch
) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"one"])])

        def broken(cls, path):
            raise RuntimeError("bad picture")

        monkeypatch.setattr(extract._Ocr, "read", classmethod(broken))
        first = extract_pending(conn, test_db)
        assert (first.failed, first.stopped) == (1, None)
        assert extract_pending(conn, test_db).failed == 0  # not read again
        assert PART not in _item(conn)[1]


def test_an_ocr_engine_that_cannot_start_stops_the_run(test_db, fake_ocr, monkeypatch) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"one", b"two"])])

        def no_engine(cls, path):
            raise extract.EngineUnavailable("model could not be fetched")

        monkeypatch.setattr(extract._Ocr, "read", classmethod(no_engine))
        counts = extract_pending(conn, test_db)
        assert counts.stopped and counts.failed == 0
        assert conn.execute("SELECT count(*) FROM blob_text").fetchone()[0] == 0
        monkeypatch.setattr(extract._Ocr, "read", classmethod(lambda cls, p: "read"))
        assert extract_pending(conn, test_db).files == 2


def test_a_deck_made_again_reads_only_its_newest_slides(test_db, fake_ocr) -> None:
    """The old export's slides stay as media beside the new ones at the same places."""
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"old one", b"old two"])])
        extract_pending(conn, test_db)
        SyncStore(conn, "notebooklm").run([_deck(store, [b"new one", b"new two"])])
        extract_pending(conn, test_db)
        content = _item(conn)[1]
        # And an import of the old export again changes nothing
        SyncStore(conn, "notebooklm").run([_deck(store, [b"old one", b"old two"])])
        assert _item(conn)[1] == content
    assert content.endswith("## Slide 1\n\nnew one\n\n## Slide 2\n\nnew two")


def test_a_time_budget_leaves_the_rest_for_next_time(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"a", b"b", b"c"])])
        first = extract_pending(conn, test_db, budget_s=-1)  # out of time at once
        assert (first.files, first.pending) == (0, 3)
        assert extract_pending(conn, test_db).files == 3


def test_other_items_get_no_text_part(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    item = _deck(store, [b"x"])
    item.metadata = {"type": "artifact", "artifact_type": "audio_overview"}
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([item])
        assert extract_pending(conn, test_db).files == 0
        assert PART not in _item(conn)[1]


def test_base_content_and_compose() -> None:
    texts = {("a" * 64, extract.OCR_TOOL): ("text a", "v1")}
    slide = Media("image", "image/png", 1, "Slide 1", "a" * 64)
    built, versions = compose("own", [slide], texts)
    assert built == f"own\n\n{PART}\n\n## Slide 1\n\ntext a" and versions == ["v1"]
    assert base_content(built) == "own"
    assert compose(built, [slide], texts)[0] == built  # rebuilding gives the same
    assert compose(built, [], {})[0] == "own"  # no files with text: the part goes


def test_page_text_skips_scripts() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as d:
        page = Path(d) / "p.html"
        page.write_text("<p>a</p><script>b</script><svg><text>c</text></svg><p>d</p>")
        assert page_text(page) == "a\nd"

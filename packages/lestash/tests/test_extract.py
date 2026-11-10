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
    real = extract.tool_version
    monkeypatch.setattr(
        extract,
        "tool_version",
        lambda tool: "fake-ocr 1" if tool == extract.OCR_TOOL else real(tool),
    )
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


def _picky(cls, path):
    data = path.read_bytes().decode()
    if data.startswith("bad"):
        raise RuntimeError("bad picture")
    return data


def test_a_file_that_cannot_be_read_is_recorded_and_read_again_by_a_new_version(
    test_db, fake_ocr, monkeypatch
) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"good", b"bad", b"fine"])])
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        first = extract_pending(conn, test_db)
        assert (first.files, first.failed, first.stopped) == (2, 1, None)
        assert extract_pending(conn, test_db).failed == 0  # recorded: not read again
        assert extract_pending(conn, test_db, retry_failed=True).failed == 1
        monkeypatch.setattr(extract, "tool_version", lambda tool: "rapidocr 9")
        assert extract_pending(conn, test_db).failed == 1  # a new version tries it


def test_a_run_whose_first_reads_all_fail_records_nothing(test_db, fake_ocr, monkeypatch) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run([_deck(store, [b"bad 1", b"bad 2", b"bad 3"])])
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        counts = extract_pending(conn, test_db)
        assert counts.engine_failed and counts.failed == 3
        assert conn.execute("SELECT count(*) FROM blob_text").fetchone()[0] == 0


def test_the_ocr_engine_names_its_version(monkeypatch, tmp_path) -> None:
    """The real reader, with RapidOCR stood in for: its version comes from the package."""
    import sys
    import types

    calls = []

    class FakeRapidOCR:
        def __init__(self, params):
            calls.append(params)

        def __call__(self, path):
            return types.SimpleNamespace(txts=(" Hello ", "", "world"))

    fake = types.SimpleNamespace(
        RapidOCR=FakeRapidOCR,
        LangRec=types.SimpleNamespace(EN="en"),
        OCRVersion=types.SimpleNamespace(PPOCRV5="v5"),
    )
    monkeypatch.setitem(sys.modules, "rapidocr", fake)
    monkeypatch.setattr(extract._Ocr, "_engine", None)
    picture = tmp_path / "p.png"
    picture.write_bytes(b"x")
    text, version = extract.read_file(extract.OCR_TOOL, picture)
    assert text == "Hello\nworld" and len(calls) == 1
    assert version.startswith("rapidocr 3.") and "en" in version
    extract.read_file(extract.OCR_TOOL, picture)
    assert len(calls) == 1  # loaded once


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


def test_bad_files_first_do_not_stop_the_good_ones(test_db, fake_ocr, monkeypatch) -> None:
    """Files are read in hash order, so the same bad ones come first every run."""
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        SyncStore(conn, "notebooklm").run(
            [_deck(store, [b"bad 1", b"bad 2", b"bad 3", b"bad 4", b"good"])]
        )
        monkeypatch.setattr(extract._Ocr, "read", classmethod(_picky))
        counts = extract_pending(conn, test_db)
        assert (counts.files, counts.failed, counts.engine_failed) == (1, 4, False)
        assert extract_pending(conn, test_db).failed == 0  # all recorded


def test_a_file_missing_from_the_store_is_skipped(test_db, fake_ocr) -> None:
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        deck = _deck(store, [b"gone", b"here"])
        SyncStore(conn, "notebooklm").run([deck])
        store.path(deck.media[0].sha256).unlink()
        counts = extract_pending(conn, test_db)
    assert (counts.files, counts.failed) == (1, 0)

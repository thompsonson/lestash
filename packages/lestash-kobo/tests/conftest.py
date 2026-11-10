"""Fixtures for lestash-kobo: a fake Kobo device and a temporary LeStash database."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import init_database

NN = "f8a3-nn-beginners"
DRIVE_PDF = "file:///mnt/onboard/.kobo/google_drive/Papers/Judge.pdf"
STORE_UNOPENED = "a1b2-never-opened"
NOTEBOOK = "file:///mnt/onboard/My Notebooks/lec001.nebo"

CONTENT_COLS = (
    "ContentID, ContentType, BookID, Title, Attribution, Description, MimeType, "
    "IsDownloaded, ___PercentRead, ReadStatus, DateLastRead, DateAdded, ___SyncTime, "
    "Publisher, ISBN, VolumeIndex, TimeSpentReading"
)


def write_kobo_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE content ({CONTENT_COLS})")
    conn.execute(
        "CREATE TABLE Bookmark (BookmarkID, VolumeID, ContentID, StartContainerPath, Text, "
        "Annotation, DateCreated, DateModified, ChapterProgress, Hidden, Type)"
    )
    rows = [
        (
            NN,
            "6",
            None,
            "Neural Networks for Beginners",
            "Brian Murray",
            "<p>An easy &amp; gentle intro.</p>",
            "application/x-kobo-epub+zip",
            "true",
            40,
            1,
            "2024-10-04T02:07:29Z",
            None,
            "2024-10-03T14:38:28Z",
            "​May Reads",
            "9798224814305",
            None,
            3600,
        ),
        (
            f"{NN}!OEBPS!c002.xhtml",
            "9",
            NN,
            "c002.xhtml",
            None,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            2,
            0,
        ),
        (
            DRIVE_PDF,
            "6",
            None,
            "Judge",
            None,
            None,
            "application/pdf",
            "true",
            12,
            1,
            "2025-09-30T08:48:03Z",
            None,
            "2025-09-29T19:00:00Z",
            None,
            None,
            None,
            0,
        ),
        (
            STORE_UNOPENED,
            "6",
            None,
            "Unopened Store Book",
            "Someone",
            None,
            "application/x-kobo-epub+zip",
            "false",
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
        ),
        (
            NOTEBOOK,
            "6",
            None,
            "lec001",
            None,
            None,
            "application/x-nebo",
            "true",
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
        ),
    ]
    conn.executemany(f"INSERT INTO content ({CONTENT_COLS}) VALUES ({','.join('?' * 17)})", rows)
    marks = [
        (
            "hl-2",
            NN,
            f"{NN}!OEBPS!c002.xhtml",
            "span#k2",
            "second",
            None,
            "2024-10-03T14:53:53.000",
            None,
            0.5,
            "false",
            "highlight",
        ),
        (
            "hl-1",
            NN,
            f"{NN}!OEBPS!c002.xhtml",
            "span#k1",
            "first",
            "my note",
            "2024-10-03T14:51:19.000",
            None,
            0.1,
            "false",
            "highlight",
        ),
        (
            "mk-1",
            NN,
            f"{NN}!OEBPS!c002.xhtml",
            None,
            None,
            None,
            "2024-10-03T14:51:20.000",
            None,
            0.2,
            "false",
            "markup",
        ),
        (
            "dg-1",
            DRIVE_PDF,
            DRIVE_PDF,
            None,
            None,
            None,
            "2025-09-30T07:00:00.000",
            None,
            0.3,
            "false",
            "dogear",
        ),
        (
            "hidden-1",
            NN,
            f"{NN}!OEBPS!c002.xhtml",
            None,
            "gone",
            None,
            "2024-10-03T15:00:00.000",
            None,
            0.9,
            "true",
            "highlight",
        ),
    ]
    conn.executemany(f"INSERT INTO Bookmark VALUES ({','.join('?' * 11)})", marks)
    # The real Bookmark.ExtraAnnotationData holds binary in a TEXT column
    conn.execute("ALTER TABLE Bookmark ADD COLUMN ExtraAnnotationData")
    conn.execute("UPDATE Bookmark SET ExtraAnnotationData = CAST(x'ff00fe' AS TEXT)")
    conn.commit()
    conn.close()


@pytest.fixture
def device(tmp_path: Path) -> Path:
    """A Kobo's root folder, as mounted at /media/kobo."""
    root = tmp_path / "device"
    write_kobo_db(root / ".kobo" / "KoboReader.sqlite")
    (root / ".kobo" / "markups").mkdir(parents=True)
    (root / ".kobo" / "markups" / "mk-1.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / ".kobo" / "markups" / "mk-1.svg").write_text("<svg/>")
    pdf = root / ".kobo" / "google_drive" / "Papers" / "Judge.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 clean")
    (root / "._junk").write_bytes(b"appledouble")
    (root / ".Spotlight-V100").mkdir()
    (root / ".Spotlight-V100" / "index").write_bytes(b"x")
    return root


@pytest.fixture(autouse=True)
def test_db(monkeypatch):
    """A temporary LeStash database, for every test: the source writes into the file
    store beside it, never into the real one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "test.db")))
        init_database(config)
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
        yield config

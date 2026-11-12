"""Photos and documents from a LinkedIn export, onto their posts and articles."""

import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.blobs import BlobStore
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_linkedin.export_media import import_media, read_uploads

RICH_MEDIA = """Date/Time,Media Description,Media Link
"You uploaded a feed photo on February 12, 2024 at 7:34 AM (GMT)", -,https://cdn/a.jpg
"You uploaded a feed photo on February 12, 2024 at 7:36 AM (GMT)", -,https://cdn/b.jpg
"You uploaded a feed document on June 17, 2025 at 5:44 PM (GMT)", -,https://cdn/doc
"You uploaded a article cover photo on February 7, 2024 at 9:02 PM (GMT)", -,https://cdn/c.jpg
"You uploaded a feed photo on July 22, 2026 at 7:19 AM (GMT)","Fun with Gemini, not saying it is right",https://cdn/g.jpg
"You uploaded a feed photo on June 11, 2026 at 5:22 AM (GMT)",A post with no link given,
"You uploaded a photo on June 25, 2024 at 11:01 AM (GMT)", -,https://cdn/profile.jpg
"You uploaded a video on June 15, 2026 at 4:43 AM (GMT)", -,https://cdn/playlist
"You uploaded a feed photo on January 1, 2020 at 9:00 AM (GMT)", -,https://cdn/lonely.jpg
"""
FILES = {
    "https://cdn/a.jpg": (b"A", "image/jpeg"),
    "https://cdn/b.jpg": (b"B", "image/jpeg"),
    "https://cdn/doc": (b"%PDF", "application/pdf"),
    "https://cdn/c.jpg": (b"C", "image/png"),
    "https://cdn/g.jpg": (b"G", "image/jpeg"),
}


def fetch(url: str) -> tuple[bytes, str]:
    return FILES[url]


@pytest.fixture
def config() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "t.db")))
        init_database(config)
        yield config


@pytest.fixture
def export(tmp_path: Path) -> zipfile.ZipFile:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Rich_Media.csv", RICH_MEDIA)
    return zipfile.ZipFile(path)


def _post(conn, n: int, at: str, text: str, resource: str = "ugcPosts") -> int:
    return upsert_item(
        conn,
        ItemCreate(
            source_type="linkedin",
            source_id=f"post-{n}",
            content=text,
            created_at=at,
            is_own_content=True,
            metadata={"resource_name": resource},
        ),
    )


def _media(conn, item_id: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT media_type, mime_type, position, alt_text FROM item_media "
            "WHERE item_id = ? ORDER BY position",
            (item_id,),
        )
    ]


def test_read_uploads(export: zipfile.ZipFile) -> None:
    uploads = read_uploads(export)
    assert len(uploads) == 9
    assert uploads[0].kind == "feed photo"
    assert uploads[0].when.isoformat() == "2024-02-12T07:34:00+00:00"
    assert uploads[3].kind == "article cover photo"


def test_uploads_go_to_their_posts(config: Config, tmp_path: Path, export) -> None:
    store = BlobStore(tmp_path / "store")
    with get_connection(config) as conn:
        cannes = _post(conn, 1, "2024-02-12 07:37:54+00:00", "Back to a clean desk")
        other = _post(conn, 2, "2024-02-12 08:45:00+00:00", "An hour later")
        talk = _post(conn, 3, "2025-06-17 17:46:00+00:00", "Slides")
        article = _post(conn, 4, "2024-02-07 21:03:00+00:00", "Guide", "article")
        # Synced in local time without a zone (#236): found by its text, not its time
        gemini = _post(
            conn,
            5,
            "2026-07-22 09:21:00",
            "Fun with Gemini, not saying it is right...",
        )
        report = import_media(conn, export, store, fetch)

        assert (report.added, report.unmatched, report.no_link) == (5, 1, 1)
        assert (report.not_post_media, report.videos, report.failed) == (1, 1, 0)
        assert _media(conn, cannes) == [
            ("image", "image/jpeg", 0, None),
            ("image", "image/jpeg", 1, None),
        ]
        assert _media(conn, other) == []
        assert _media(conn, talk) == [("pdf", "application/pdf", 0, None)]
        assert _media(conn, article) == [("image", "image/png", 0, None)]
        # The description is the post's text: not repeated as the photo's alt text
        assert _media(conn, gemini) == [("image", "image/jpeg", 0, None)]
        sha = conn.execute("SELECT sha256 FROM item_media WHERE item_id = ?", (talk,)).fetchone()[0]
        assert store.path(sha).read_bytes() == b"%PDF"


def test_import_twice_adds_nothing(config: Config, tmp_path: Path, export) -> None:
    store = BlobStore(tmp_path / "store")
    with get_connection(config) as conn:
        _post(conn, 1, "2024-02-12 07:37:54+00:00", "Back to a clean desk")
        assert import_media(conn, export, store, fetch).added == 2
        again = import_media(conn, export, store, fetch)
        assert (again.added, again.held) == (0, 2)
        assert conn.execute("SELECT count(*) FROM item_media").fetchone()[0] == 2


def test_a_failed_download_is_counted_and_tried_next_time(
    config: Config, tmp_path: Path, export
) -> None:
    def broken(url: str) -> tuple[bytes, str]:
        raise OSError("link expired")

    with get_connection(config) as conn:
        _post(conn, 1, "2024-02-12 07:37:54+00:00", "Back to a clean desk")
        report = import_media(conn, export, BlobStore(tmp_path / "store"), broken)
        assert (report.added, report.failed) == (0, 2)
        assert conn.execute("SELECT count(*) FROM item_media").fetchone()[0] == 0

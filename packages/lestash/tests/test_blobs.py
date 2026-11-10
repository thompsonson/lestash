"""The file store (#217): media files stored once by SHA-256, beside the test DB."""

from __future__ import annotations

from pathlib import Path

from lestash.core.blobs import BlobStore, adopt_media, media_file, sha_file, store_root
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import (
    SCHEMA_VERSION,
    add_item_media,
    apply_migrations,
    get_connection,
    get_media_dir,
)


def _item(conn) -> int:
    return conn.execute(
        "INSERT INTO items (source_type, content) VALUES ('t', 'x') RETURNING id"
    ).fetchone()[0]


def _media(conn, config: Config, item_id: int, rel: str, data: bytes) -> int:
    path = get_media_dir(config) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return add_item_media(conn, item_id, media_type="image", local_path=rel, mime_type="image/png")


def test_a_test_database_has_its_own_store(test_db) -> None:
    db = Path(test_db.general.database_path)
    assert store_root(test_db) == db.parent / "store"
    chosen = Config(general=GeneralConfig(database_path=str(db), store_path="/x/y"))
    assert store_root(chosen) == Path("/x/y")


def test_a_file_is_stored_once(tmp_path) -> None:
    store = BlobStore(tmp_path / "store")
    one, two = tmp_path / "a.png", tmp_path / "b.png"
    one.write_bytes(b"same")
    two.write_bytes(b"same")
    first, second = store.put_file(one), store.put_file(two)
    assert first.new and not second.new
    assert first.sha256 == second.sha256 == sha_file(one)
    assert store.path(first.sha256).read_bytes() == b"same"
    assert list((tmp_path / "store" / "blobs").rglob(".tmp-*")) == []


def test_adopting_points_media_at_the_store_and_runs_again_cleanly(test_db) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        a = _media(conn, test_db, item, f"{item}/a.png", b"png a")
        b = _media(conn, test_db, item, f"{item}/b.png", b"png a")  # the same bytes
        gone = add_item_media(conn, item, media_type="image", local_path=f"{item}/gone.png")
        counts = adopt_media(conn, test_db)
        assert (counts.adopted, counts.new_blobs, counts.new_bytes, counts.missing) == (2, 1, 5, 1)
        rows = dict(conn.execute("SELECT id, sha256 FROM item_media").fetchall())
        assert rows[a] == rows[b] is not None and rows[gone] is None
        assert tuple(conn.execute("SELECT size, mime_type FROM blobs").fetchone()) == (
            5,
            "image/png",
        )
        again = adopt_media(conn, test_db)
        assert (again.adopted, again.new_blobs, again.missing) == (0, 0, 1)
        # The media folder's file can go: the store still has it
        row = dict(conn.execute("SELECT * FROM item_media WHERE id = ?", (a,)).fetchone())
        (get_media_dir(test_db) / row["local_path"]).unlink()
        found = media_file(row, test_db)
        assert found is not None and found.read_bytes() == b"png a"
        assert found.is_relative_to(store_root(test_db))


def test_migration_22_adds_blobs_and_media_versions(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.executescript(
            "DROP INDEX idx_item_media_sha256; ALTER TABLE item_media DROP COLUMN sha256; "
            "ALTER TABLE item_media DROP COLUMN version; "
            "ALTER TABLE item_media DROP COLUMN superseded_at; DROP TABLE blobs; "
            "PRAGMA user_version = 21;"
        )
        assert apply_migrations(conn) == 1
        columns = {r[1] for r in conn.execute("PRAGMA table_info(item_media)")}
        assert {"sha256", "version", "superseded_at"} <= columns
        assert apply_migrations(conn) == 0
        assert SCHEMA_VERSION == 22


def test_a_file_that_cannot_be_read_is_skipped_and_tried_again(test_db, monkeypatch) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        bad = _media(conn, test_db, item, f"{item}/bad.png", b"bad")
        good = _media(conn, test_db, item, f"{item}/good.png", b"good")
        real_put = BlobStore.put_file

        def put_file(self, src):
            if src.name == "bad.png":
                raise OSError("unreadable")
            return real_put(self, src)

        monkeypatch.setattr(BlobStore, "put_file", put_file)
        counts = adopt_media(conn, test_db)
        assert (counts.adopted, counts.failed) == (1, 1)
        assert not conn.in_transaction  # committed: no write lock held
        rows = dict(conn.execute("SELECT id, sha256 FROM item_media").fetchall())
        assert rows[bad] is None and rows[good] is not None
        monkeypatch.setattr(BlobStore, "put_file", real_put)
        assert adopt_media(conn, test_db, ids=[bad]).adopted == 1


def test_media_added_again_by_path_after_adopting_is_not_doubled(test_db) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        adopt_media(conn, test_db)
        # A sync names the same file again by its path, as the Kobo reader does
        add_item_media(conn, item, media_type="image", local_path=f"{item}/a.png")
        assert conn.execute("SELECT count(*) FROM item_media").fetchone()[0] == 1


def test_media_versions_are_matched_on_the_file_and_when_it_was_first_seen(test_db) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        # An adopted markup from before versions: a media-folder file, also in the store
        first = _media(conn, test_db, item, "kobo/markup-a.jpg", b"one")
        adopt_media(conn, test_db)
        sha_one = conn.execute("SELECT sha256 FROM item_media").fetchone()[0]

        def put(sha: str, seen: str, superseded: str | None, origin: str = "sync") -> int:
            return add_item_media(
                conn,
                item,
                media_type="image",
                mime_type="image/png",
                source_origin=origin,
                sha256=sha,
                size=3,
                seen_at=seen,
                superseded_at=superseded,
            )

        t1, t2, t3 = "2026-11-07 09:56:00", "2026-11-07 09:57:00", "2026-11-07 09:58:00"
        assert put(sha_one, t1, None) == first  # the same file, current: the same row
        before = conn.total_changes
        put(sha_one, t1, None)
        assert conn.total_changes == before  # nothing differs, nothing is written
        # Redrawn, then back to the first drawing: three versions, numbered as they came
        put(sha_one, t1, t2)
        second = put("b" * 64, t2, t3)
        third = put(sha_one, t3, None)
        rows = conn.execute(
            "SELECT id, version, superseded_at, local_path FROM item_media ORDER BY version"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            (first, 1, t2, "kobo/markup-a.jpg"),
            (second, 2, t3, None),
            (third, 3, None, None),
        ]
        # The source forgets the first sighting (a lost snapshot): nothing renumbers,
        # and no second current row appears
        put("b" * 64, t2, t3)
        put(sha_one, t3, None)
        assert conn.execute("SELECT count(*) FROM item_media").fetchone()[0] == 3


def test_a_versioned_import_never_takes_an_upload_at_the_same_place(test_db) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        upload = add_item_media(
            conn, item, media_type="pdf", local_path="1/mine.pdf", source_origin="upload"
        )
        add_item_media(
            conn,
            item,
            media_type="pdf",
            sha256="a" * 64,
            size=1,
            seen_at="2026-11-07 09:56:00",
        )
        rows = conn.execute(
            "SELECT id, source_origin, local_path, sha256 FROM item_media ORDER BY id"
        ).fetchall()
    assert tuple(rows[0]) == (upload, "upload", "1/mine.pdf", None)
    assert len(rows) == 2 and rows[1][1] == "sync"

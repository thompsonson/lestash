"""The file store (#217): media files stored once by SHA-256, beside the test DB."""

from __future__ import annotations

from pathlib import Path

from lestash.core.blobs import (
    BlobStore,
    adopt_media,
    media_file,
    prune_media,
    sha_file,
    store_root,
)
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
        # It forgets the middle one: the two sightings of the first drawing look like
        # one, current since t1. The first row stays replaced.
        put(sha_one, t1, None)
        current = conn.execute("SELECT id FROM item_media WHERE superseded_at IS NULL")
        assert [r[0] for r in current] == [third]


def test_an_adopted_row_redrawn_before_its_first_versioned_import(test_db) -> None:
    with get_connection(test_db) as conn:
        item = _item(conn)
        first = _media(conn, test_db, item, "kobo/markup-a.jpg", b"one")
        adopt_media(conn, test_db)
        sha_one = conn.execute("SELECT sha256 FROM item_media").fetchone()[0]
        t1, t2 = "2026-11-07 09:56:00", "2026-11-07 09:57:00"
        for sha, seen, superseded in ((sha_one, t1, t2), ("b" * 64, t2, None)):
            add_item_media(
                conn,
                item,
                media_type="image",
                sha256=sha,
                size=3,
                seen_at=seen,
                superseded_at=superseded,
            )
        rows = conn.execute(
            "SELECT id, version, superseded_at FROM item_media ORDER BY version"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(first, 1, t2), (rows[1][0], 2, None)]


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


def test_a_lost_newest_version_does_not_stay_current(test_db) -> None:
    t1, t2, t3 = "2026-11-07 09:56:00", "2026-11-07 09:57:00", "2026-11-07 09:58:00"
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    with get_connection(test_db) as conn:
        item = _item(conn)

        def put(sha: str, seen: str, superseded: str | None) -> int:
            return add_item_media(
                conn,
                item,
                media_type="pdf",
                sha256=sha,
                size=1,
                seen_at=seen,
                superseded_at=superseded,
            )

        def current() -> list[str]:
            return [
                r[0]
                for r in conn.execute("SELECT sha256 FROM item_media WHERE superseded_at IS NULL")
            ]

        put(a, t1, t2)
        put(b, t2, None)  # B imported as current; then its snapshot is lost
        put(a, t1, t3)
        put(c, t3, None)
        assert current() == [c]
        (closed,) = conn.execute("SELECT superseded_at FROM item_media WHERE sha256 = ?", (b,))
        assert closed[0] == t3  # replaced when C was seen
        # A-B-A, B's snapshot lost: the source says A is current since t1
        put(a, t1, None)
        assert current() == [a]
        before = conn.total_changes
        put(a, t1, None)
        assert conn.total_changes == before  # and stays so, writing nothing


def test_pruning_removes_only_copies_checked_against_the_store(test_db) -> None:
    media = get_media_dir(test_db)
    with get_connection(test_db) as conn:
        item = _item(conn)
        stored = _media(conn, test_db, item, f"{item}/a.png", b"png a")
        _media(conn, test_db, item, f"{item}/twin.png", b"png a")  # same bytes, own name
        adopt_media(conn, test_db)
        _media(conn, test_db, item, f"{item}/new.png", b"not adopted yet")
        (media / f"{item}/unnamed.png").write_bytes(b"no row names this")
        # A row whose file changed after it was stored: the copy is not its blob
        changed = _media(conn, test_db, item, f"{item}/changed.png", b"before")
        adopt_media(conn, test_db, ids=[changed])
        (media / f"{item}/changed.png").write_bytes(b"after")
        conn.commit()

        counted = prune_media(conn, test_db, delete=False)
        assert (counted.removed, counted.kept) == (2, 3)
        assert (media / f"{item}/a.png").is_file()  # counting removes nothing

        counts = prune_media(conn, test_db)
        assert (counts.removed, counts.removed_bytes, counts.kept) == (2, 10, 3)
        left = sorted(p.name for p in (media / str(item)).iterdir())
        assert left == ["changed.png", "new.png", "unnamed.png"]
        # A pruned file is still served, from the store
        row = dict(conn.execute("SELECT * FROM item_media WHERE id = ?", (stored,)).fetchone())
        assert media_file(row, test_db).read_bytes() == b"png a"
        assert prune_media(conn, test_db).removed == 0  # runs again cleanly


def test_pruning_keeps_a_copy_whose_stored_file_is_missing_or_damaged(test_db) -> None:
    media = get_media_dir(test_db)
    store = BlobStore(store_root(test_db))
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/lost.png", b"lost")
        _media(conn, test_db, item, f"{item}/bad.png", b"bad")
        adopt_media(conn, test_db)
        conn.commit()
        shas = dict(conn.execute("SELECT local_path, sha256 FROM item_media").fetchall())
        store.path(shas[f"{item}/lost.png"]).unlink()
        store.path(shas[f"{item}/bad.png"]).write_bytes(b"damaged")
        counts = prune_media(conn, test_db)
    assert (counts.removed, counts.kept) == (0, 2)
    assert (media / f"{item}/lost.png").is_file() and (media / f"{item}/bad.png").is_file()


def test_pruning_removes_emptied_item_folders(test_db) -> None:
    media = get_media_dir(test_db)
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        adopt_media(conn, test_db)
        conn.commit()
        prune_media(conn, test_db)
    assert not (media / str(item)).exists() and media.is_dir()


def test_a_file_named_by_an_adopted_and_an_unadopted_row_is_kept(test_db) -> None:
    media = get_media_dir(test_db)
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        adopt_media(conn, test_db)
        # A writer's new row for the same file, recorded before the next adopt
        add_item_media(conn, item, media_type="pdf", local_path=f"{item}/a.png")
        conn.commit()
        assert prune_media(conn, test_db).kept == 1
    assert (media / f"{item}/a.png").is_file()


def test_a_row_recorded_while_its_file_was_pruned_is_repaired(test_db) -> None:
    """The race prune's re-check narrows: a writer finds the file, prune removes it,
    then the writer records a row. The next adopt points that row at the stored file."""
    from lestash.core.database import save_media_file

    with get_connection(test_db) as conn:
        item = _item(conn)
        rel = save_media_file(item, b"pdf bytes", "doc.pdf", test_db)
        add_item_media(conn, item, media_type="image", local_path=rel)
        conn.commit()
        adopt_media(conn, test_db)
        prune_media(conn, test_db)
        late = add_item_media(conn, item, media_type="pdf", local_path=rel)
        conn.commit()
        counts = adopt_media(conn, test_db)
        row = dict(conn.execute("SELECT * FROM item_media WHERE id = ?", (late,)).fetchone())
    assert (counts.adopted, counts.missing) == (1, 0)
    assert media_file(row, test_db).read_bytes() == b"pdf bytes"


def test_a_media_folder_shared_with_another_database_is_not_pruned(test_db) -> None:
    db = Path(test_db.general.database_path)
    (db.parent / "other.db").write_bytes(b"")
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        adopt_media(conn, test_db)
        conn.commit()
        counts = prune_media(conn, test_db)
    assert counts.removed == 0 and (get_media_dir(test_db) / f"{item}/a.png").is_file()


def test_a_database_restored_from_before_the_prune_finds_its_files(test_db) -> None:
    """Its rows have no sha256 and their media-folder copies are gone; each file is
    found in the store by its name, the start of its hash."""
    from lestash.core.database import save_media_file

    with get_connection(test_db) as conn:
        item = _item(conn)
        rel = save_media_file(item, b"kept bytes", "a.png", test_db)
        media_id = add_item_media(conn, item, media_type="image", local_path=rel)
        conn.commit()
        adopt_media(conn, test_db)
        prune_media(conn, test_db)
        # As in a backup from before the first adopt: no hashes, no blobs rows
        conn.execute("UPDATE item_media SET sha256 = NULL")
        conn.execute("DELETE FROM blobs")
        healthy = _media(conn, test_db, item, f"{item}/b.png", b"new bytes")
        conn.commit()
        counts = adopt_media(conn, test_db)
        row = dict(conn.execute("SELECT * FROM item_media WHERE id = ?", (media_id,)).fetchone())
        (other,) = conn.execute("SELECT sha256 FROM item_media WHERE id = ?", (healthy,)).fetchone()
    assert (counts.adopted, counts.missing) == (2, 0) and other
    assert media_file(row, test_db).read_bytes() == b"kept bytes"


def test_a_managed_backup_beside_the_database_does_not_stop_pruning(test_db) -> None:
    db = Path(test_db.general.database_path)
    (db.parent / "lestash-backup-20261110T110000.db").write_bytes(b"")
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        adopt_media(conn, test_db)
        conn.commit()
        counts = prune_media(conn, test_db)
    assert counts.removed == 1 and counts.shared_with is None


def test_a_row_moved_to_another_file_with_the_old_hash_is_stored_again(test_db) -> None:
    """Left by set_source_pdf before #217 step 4: the store served the old file."""
    from lestash.core.database import save_media_file

    with get_connection(test_db) as conn:
        item = _item(conn)
        old = save_media_file(item, b"old pdf", "a.pdf", test_db)
        media_id = add_item_media(conn, item, media_type="source_pdf", local_path=old)
        conn.commit()
        adopt_media(conn, test_db)
        new = save_media_file(item, b"new pdf", "a.pdf", test_db)
        conn.execute("UPDATE item_media SET local_path = ? WHERE id = ?", (new, media_id))
        conn.commit()
        adopt_media(conn, test_db)
        row = dict(conn.execute("SELECT * FROM item_media WHERE id = ?", (media_id,)).fetchone())
    assert media_file(row, test_db).read_bytes() == b"new pdf"


def test_a_busy_write_lock_stops_the_prune_and_keeps_the_file(test_db, monkeypatch) -> None:
    import sqlite3

    from lestash.core import database

    media = get_media_dir(test_db)
    with get_connection(test_db) as conn:
        item = _item(conn)
        _media(conn, test_db, item, f"{item}/a.png", b"png a")
        _media(conn, test_db, item, f"{item}/b.png", b"png b")
        adopt_media(conn, test_db)
        conn.commit()
    monkeypatch.setattr(database, "BUSY_TIMEOUT_S", 0.1)
    holder = sqlite3.connect(test_db.general.database_path)
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    try:
        with get_connection(test_db) as conn:
            counts = prune_media(conn, test_db)
    finally:
        holder.rollback()
        holder.close()
    assert counts.stopped and counts.removed == 0
    assert (media / f"{item}/a.png").is_file() and (media / f"{item}/b.png").is_file()
    with get_connection(test_db) as conn:
        assert prune_media(conn, test_db).removed == 2  # the next run goes on

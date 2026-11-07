"""The snapshot store: versioned, de-duplicated, all-or-nothing."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lestash_kobo import store as store_mod
from lestash_kobo.store import KoboStore, device_name

T1 = datetime(2026, 4, 17, 14, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 7, 9, 56, tzinfo=UTC)
T3 = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
PDF = ".kobo/google_drive/Papers/Judge.pdf"


def test_snapshot_copies_device_and_skips_os_junk(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    result = store.take(device, T1)
    assert set(result.snapshot.files) == {
        ".kobo/KoboReader.sqlite",
        ".kobo/markups/mk-1.jpg",
        ".kobo/markups/mk-1.svg",
        PDF,
    }
    assert result.new_blobs == 4
    blob = result.snapshot.blob(store, PDF)
    assert blob is not None and blob.read_bytes() == b"%PDF-1.4 clean"
    assert not os.access(blob, os.W_OK)  # blobs are read-only


def test_unchanged_device_adds_no_blobs(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    again = store.take(device, T2)
    assert again.new_blobs == 0
    assert not (again.added or again.changed or again.removed)
    assert [s.name for s in store.snapshots()] == ["20260417T140000Z", "20261107T095600Z"]


def test_annotated_pdf_keeps_every_version(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    first = store.take(device, T1).snapshot
    (device / PDF).write_bytes(b"%PDF-1.4 with ink")
    second = store.take(device, T2)
    assert second.changed == [PDF] and second.new_blobs == 1
    old, new = first.blob(store, PDF), second.snapshot.blob(store, PDF)
    assert old is not None and new is not None
    assert old.read_bytes() == b"%PDF-1.4 clean"
    assert new.read_bytes() == b"%PDF-1.4 with ink"


def test_pdf_reverting_to_first_version_is_flagged(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    (device / PDF).write_bytes(b"%PDF-1.4 with ink")
    store.take(device, T2)
    (device / PDF).write_bytes(b"%PDF-1.4 clean")  # Drive sync put the original back
    third = store.take(device, T3)
    assert third.reverted == [PDF]


def test_failed_snapshot_leaves_nothing(device: Path, tmp_path: Path, monkeypatch) -> None:
    store = KoboStore(tmp_path / "store")
    calls = 0
    real = KoboStore._store_blob

    def unplugged(self, path):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("device gone")
        return real(self, path)

    monkeypatch.setattr(KoboStore, "_store_blob", unplugged)
    with pytest.raises(OSError):
        store.take(device, T1)
    assert store.snapshots() == []
    assert not [p for p in store.snapshots_dir.iterdir()]  # no temp folder left
    assert not list(store.blobs.glob(".tmp-*"))


def test_snapshot_needs_a_kobo(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="is the Kobo mounted"):
        KoboStore(tmp_path / "store").take(tmp_path)


def test_same_time_twice_is_refused(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    with pytest.raises(FileExistsError):
        store.take(device, T1)


def test_latin1_mount_names_are_repaired() -> None:
    # A Latin-1 mount turns "·" into byte 0xB7, read by Python as a lone surrogate
    raw = os.fsdecode("a \xb7 b.pdf".encode("latin-1"))
    assert device_name(raw) == "a · b.pdf"
    assert device_name("plain é.pdf") == "plain é.pdf"


def test_default_root_is_under_local_share() -> None:
    assert store_mod.DEFAULT_ROOT.parts[-3:] == ("share", "lestash", "kobo")


def test_unchanged_files_are_not_read_again(device: Path, tmp_path: Path, monkeypatch) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    (device / PDF).write_bytes(b"%PDF-1.4 with ink")
    read: list[str] = []
    real = KoboStore._store_blob

    def spy(self, path):
        read.append(path.name)
        return real(self, path)

    monkeypatch.setattr(KoboStore, "_store_blob", spy)
    second = store.take(device, T2)
    assert read == ["Judge.pdf"]
    assert second.changed == [PDF]
    read.clear()
    store.take(device, T3, verify=True)
    assert len(read) == 4


def test_old_copy_imported_late_compares_with_what_came_before(
    device: Path, tmp_path: Path
) -> None:
    store = KoboStore(tmp_path / "store")
    (device / PDF).write_bytes(b"%PDF-1.4 with ink")
    store.take(device, T2)
    (device / PDF).write_bytes(b"%PDF-1.4 clean")
    april = store.take(device, T1)  # older than the snapshot already there
    assert not (april.added or april.changed or april.removed or april.reverted)
    assert [s.name for s in store.snapshots()] == ["20260417T140000Z", "20261107T095600Z"]


def test_leftovers_of_a_killed_copy_are_swept(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    (store.snapshots_dir / ".tmp-killed").mkdir()
    (store.snapshots_dir / ".tmp-killed" / "manifest.json").write_text("{}")
    (store.blobs / ".tmp-partial").write_bytes(b"half")
    store.take(device, T2)
    assert not list(store.snapshots_dir.glob(".tmp-*"))
    assert not list(store.blobs.glob(".tmp-*"))


def test_a_damaged_manifest_is_skipped(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    broken = store.snapshots_dir / "20260101T000000Z"
    broken.mkdir()
    (broken / "manifest.json").write_text('{"taken_at": ')
    assert [s.name for s in store.snapshots()] == ["20260417T140000Z"]
    assert store.take(device, T2).new_blobs == 0


def test_pdf_deleted_then_restored_clean_is_flagged(device: Path, tmp_path: Path) -> None:
    store = KoboStore(tmp_path / "store")
    store.take(device, T1)
    (device / PDF).write_bytes(b"%PDF-1.4 with ink")
    store.take(device, T2)
    (device / PDF).unlink()  # Drive sync removed it...
    store.take(device, T3)
    (device / PDF).write_bytes(b"%PDF-1.4 clean")  # ...then downloaded the original
    fourth = store.take(device, datetime(2026, 11, 9, tzinfo=UTC))
    assert fourth.added == [PDF] and fourth.reverted == [PDF]

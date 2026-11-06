"""Tests for the Takeout plugin: Drive listing, per-zip state committed with the items."""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from lestash.cli import sources as sources_cli
from lestash.core.database import get_checkpoints, get_connection
from lestash.importers import IMPORTER_VERSION
from lestash_takeout import source

NB = "Takeout/NotebookLM/Planning"
NOTEBOOK = {
    f"{NB}/Planning.json": json.dumps({"title": "Planning"}),
    f"{NB}/Notes/Plan A.html": "<p>first plan</p>",
}


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


class FakeDrive:
    """Just enough of the Drive v3 client for the plugin."""

    def __init__(self, files: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        self._files, self.blobs = files, blobs

    def files(self):
        return self

    def list(self, **kwargs):
        self.query = kwargs["q"]
        return _Exec({"files": self._files})


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def _file(fid: str, name: str) -> dict[str, Any]:
    return {"id": fid, "name": name, "size": "10", "createdTime": "2026-03-01"}


@pytest.fixture
def drive(tmp_path, monkeypatch):
    """A fake Drive with a NotebookLM zip (f1) and a photos-only zip (f2)."""
    service = FakeDrive(
        [_file("f1", "takeout-1-001.zip"), _file("f2", "takeout-1-002.zip"), _file("f3", "x.tgz")],
        {
            "f1": _zip(tmp_path / "good.zip", NOTEBOOK),
            "f2": _zip(tmp_path / "junk.zip", {"Takeout/Photos/a.jpg": "x"}),
        },
    )
    service.downloaded = []

    def fake_download(svc, file_id, dest):
        dest.write_bytes(svc.blobs[file_id].read_bytes())
        svc.downloaded.append(dest)
        return dest

    monkeypatch.setattr(source, "download", fake_download)
    monkeypatch.setattr(source, "_drive_service", lambda: service)
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"takeout": source.TakeoutSource()})
    return service


def _sync() -> None:
    sources_cli.sync_source(source_name="takeout", all_sources=False)


def _items(test_db) -> list[str]:
    with get_connection(test_db) as conn:
        return [r[0] for r in conn.execute("SELECT source_id FROM items ORDER BY source_id")]


def _state(test_db) -> dict[str, dict]:
    with get_connection(test_db) as conn:
        return get_checkpoints(conn, "takeout")


def test_sync_imports_new_zips_once_and_deletes_downloads(test_db, drive) -> None:
    _sync()
    assert _items(test_db) == [NB, f"{NB}/Notes/Plan A.html"]
    assert "'me' in owners" in drive.query
    # .tgz ignored; both zips downloaded and their temp copies removed
    assert [p.name for p in drive.downloaded] == ["takeout-1-001.zip", "takeout-1-002.zip"]
    assert not any(p.exists() for p in drive.downloaded)

    state = _state(test_db)
    assert state["f1"]["status"] == "imported" and state["f1"]["items"] == 2
    assert state["f2"]["status"] == "unrecognized"
    assert state["f2"]["importer_version"] == IMPORTER_VERSION

    # Second sync: nothing new to download
    drive.downloaded.clear()
    _sync()
    assert drive.downloaded == []

    # Forget one: it's imported again
    assert source.forget("f1")
    _sync()
    assert [p.name for p in drive.downloaded] == ["takeout-1-001.zip"]


def test_kill_after_a_zip_keeps_its_items_and_its_record(test_db, drive, monkeypatch) -> None:
    def download(svc, file_id, dest):
        if file_id == "f2":
            raise KeyboardInterrupt("killed while downloading the next zip")
        dest.write_bytes(svc.blobs[file_id].read_bytes())
        return dest

    monkeypatch.setattr(source, "download", download)
    with pytest.raises(KeyboardInterrupt):
        _sync()
    # Zip 1's items and its record were committed together
    assert set(_state(test_db)) == {"f1"}
    assert NB in _items(test_db)


def test_kill_inside_a_zip_leaves_neither_items_nor_record(test_db, drive, monkeypatch) -> None:
    real_parse = source.parse_zip

    class Explode(list):
        def __iter__(self):
            yield self[0]
            raise KeyboardInterrupt("killed half-way through the zip's items")

    def parse(path):
        items, kind = real_parse(path)
        return Explode(items), kind

    monkeypatch.setattr(source, "parse_zip", parse)
    with pytest.raises(KeyboardInterrupt):
        _sync()
    assert _state(test_db) == {}
    assert _items(test_db) == []


def test_a_failing_zip_does_not_stop_the_others(test_db, drive, monkeypatch) -> None:
    real = source.download

    def download(svc, file_id, dest):
        if file_id == "f1":
            raise OSError("connection reset")
        return real(svc, file_id, dest)

    monkeypatch.setattr(source, "download", download)
    _sync()
    state = _state(test_db)
    assert "f1" not in state  # retried next time
    assert state["f2"]["status"] == "unrecognized"


@pytest.mark.parametrize(
    ("record", "done"),
    [
        (None, False),
        ({"status": "imported", "importer_version": 1}, True),
        ({"status": "unrecognized", "importer_version": 1}, False),  # retried by a newer importer
        ({"status": "unrecognized", "importer_version": IMPORTER_VERSION}, True),
    ],
)
def test_is_done(record, done) -> None:
    assert source.is_done(record) is done


def test_no_google_signin_yields_nothing(monkeypatch) -> None:
    def no_creds():
        raise ValueError("No valid Google credentials")

    monkeypatch.setattr(source, "_drive_service", no_creds)
    assert list(source.TakeoutSource().sync({})) == []


def test_a_truncated_download_is_retried_not_marked_unrecognized(
    test_db, drive, monkeypatch
) -> None:
    real = source.download

    def truncated(svc, file_id, dest):
        real(svc, file_id, dest)
        if file_id == "f1":
            data = dest.read_bytes()
            dest.write_bytes(data[: len(data) // 2])
        return dest

    monkeypatch.setattr(source, "download", truncated)
    _sync()
    state = _state(test_db)
    assert "f1" not in state  # retried next time
    assert state["f2"]["status"] == "unrecognized"

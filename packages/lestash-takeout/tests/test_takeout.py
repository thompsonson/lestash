"""Tests for the Takeout plugin: Drive listing, per-zip state committed with the items."""

import errno
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest
import typer
from googleapiclient.errors import HttpError
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


def _sync(fails: bool = False) -> None:
    """Run the CLI sync, which exits 1 when the sync failed (#209)."""
    if not fails:
        sources_cli.sync_source(source_name="takeout", all_sources=False)
        return
    with pytest.raises(typer.Exit) as exc:
        sources_cli.sync_source(source_name="takeout", all_sources=False)
    assert exc.value.exit_code == 1


def _items(test_db) -> list[str]:
    with get_connection(test_db) as conn:
        return [r[0] for r in conn.execute("SELECT source_id FROM items ORDER BY source_id")]


def _state(test_db) -> dict[str, dict]:
    with get_connection(test_db) as conn:
        return get_checkpoints(conn, "takeout")


def _last_sync(test_db) -> tuple[str, str | None]:
    with get_connection(test_db) as conn:
        return tuple(
            conn.execute(
                "SELECT status, error_message FROM sync_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )


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


def test_kill_inside_a_zip_leaves_no_record(test_db, drive, monkeypatch) -> None:
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
    # Items may be committed in chunks before the record; the zip is imported again
    assert _state(test_db) == {}
    assert _items(test_db) == []


def _http_error(status: int, content: bytes = b"{}") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), content)


def test_a_zip_gone_from_drive_does_not_stop_the_others_but_fails_the_sync(
    test_db, drive, monkeypatch
) -> None:
    real = source.download

    def download(svc, file_id, dest):
        if file_id == "f1":
            raise _http_error(404)
        return real(svc, file_id, dest)

    monkeypatch.setattr(source, "download", download)
    _sync(fails=True)
    state = _state(test_db)
    assert state["f1"]["status"] == "failed" and state["f1"]["attempts"] == 1
    assert state["f2"]["status"] == "unrecognized"
    status, error = _last_sync(test_db)
    assert status == "failed"
    assert "takeout-1-001.zip" in error


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connection reset"),
        OSError(errno.ENOSPC, "No space left on device"),
        _http_error(503),
        _http_error(403, b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'),
        _http_error(403, b'{"error": {"errors": [{"reason": "downloadQuotaExceeded"}]}}'),
    ],
    ids=["network", "disk-full", "drive-5xx", "rate-limit", "download-quota"],
)
def test_a_systemic_download_error_stops_the_sync_and_counts_no_attempt(
    test_db, drive, monkeypatch, error
) -> None:
    """Sign-in, network, Drive or disk errors would fail every zip: none uses up retries."""

    def download(svc, file_id, dest):
        svc.downloaded.append(dest)
        raise error

    monkeypatch.setattr(source, "download", download)
    for _ in range(source.MAX_ATTEMPTS):
        drive.downloaded.clear()
        _sync(fails=True)
        # Stopped at the first zip: the second isn't tried
        assert [p.name for p in drive.downloaded] == ["takeout-1-001.zip"]
        assert _state(test_db) == {}
        status, message = _last_sync(test_db)
        assert status == "failed" and "takeout-1-001.zip" in message

    assert [f["name"] for f in source.pending_zips(drive, _state(test_db))] == [
        "takeout-1-001.zip",
        "takeout-1-002.zip",
    ]


def test_a_systemic_error_after_a_bad_zip_reports_both(test_db, drive, monkeypatch) -> None:
    def download(svc, file_id, dest):
        raise _http_error(404) if file_id == "f1" else ConnectionError("offline")

    monkeypatch.setattr(source, "download", download)
    _sync(fails=True)
    assert set(_state(test_db)) == {"f1"}
    status, message = _last_sync(test_db)
    assert status == "failed" and "offline" in message and "takeout-1-001.zip" in message


def test_a_zip_that_fails_to_parse_is_retried_then_given_up(test_db, drive, monkeypatch) -> None:
    real_parse = source.parse_zip

    def parse(path):
        if path.name == "takeout-1-001.zip":
            raise KeyError("parser bug")
        return real_parse(path)

    monkeypatch.setattr(source, "parse_zip", parse)
    for attempt in range(1, source.MAX_ATTEMPTS + 1):
        drive.downloaded.clear()
        _sync(fails=True)
        assert "takeout-1-001.zip" in [p.name for p in drive.downloaded]
        assert _state(test_db)["f1"]["attempts"] == attempt
        assert _last_sync(test_db)[0] == "failed"

    # Given up: not downloaded again, and the sync completes
    drive.downloaded.clear()
    _sync()
    assert drive.downloaded == []
    assert _last_sync(test_db) == ("completed", None)
    assert "gave up" in source.describe_failure(_state(test_db)["f1"])

    # forget resets it
    monkeypatch.setattr(source, "parse_zip", real_parse)
    assert source.forget("f1")
    _sync()
    assert _state(test_db)["f1"]["status"] == "imported"
    assert _last_sync(test_db) == ("completed", None)


@pytest.mark.parametrize(
    ("record", "done"),
    [
        (None, False),
        ({"status": "imported", "importer_version": 1}, True),
        ({"status": "unrecognized", "importer_version": 1}, False),  # retried by a newer importer
        ({"status": "unrecognized", "importer_version": IMPORTER_VERSION}, True),
        ({"status": "failed", "attempts": 1, "importer_version": IMPORTER_VERSION}, False),
        ({"status": "failed", "attempts": 3, "importer_version": IMPORTER_VERSION}, True),
        ({"status": "failed", "attempts": 3, "importer_version": 1}, False),  # newer importer
    ],
)
def test_is_done(record, done) -> None:
    assert source.is_done(record) is done


def test_no_google_signin_yields_nothing(monkeypatch) -> None:
    def no_creds():
        raise ValueError("No valid Google credentials")

    monkeypatch.setattr(source, "_drive_service", no_creds)
    assert list(source.TakeoutSource().sync({})) == []


def test_a_truncated_download_is_a_failure_not_unrecognized(test_db, drive, monkeypatch) -> None:
    real = source.download

    def truncated(svc, file_id, dest):
        real(svc, file_id, dest)
        if file_id == "f1":
            data = dest.read_bytes()
            dest.write_bytes(data[: len(data) // 2])
        return dest

    monkeypatch.setattr(source, "download", truncated)
    _sync(fails=True)
    state = _state(test_db)
    assert state["f1"]["status"] == "failed"  # retried next time
    assert state["f2"]["status"] == "unrecognized"


def test_a_zip_too_big_for_the_disk_is_skipped_and_the_others_import(
    test_db, drive, monkeypatch
) -> None:
    drive._files[0]["size"] = str(50 * 10**9)  # f1: far more than is free
    monkeypatch.setattr(source.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**9))
    _sync(fails=True)
    state = _state(test_db)
    assert "f1" not in state  # no attempt counted: retried once there is room
    assert drive.downloaded and all("001" not in p.name for p in drive.downloaded)
    assert state["f2"]["status"] == "unrecognized"  # the smaller zip still ran
    status, error = _last_sync(test_db)
    assert status == "failed"
    assert "takeout-1-001.zip" in error and "GB free" in error


ART = f"{NB}/Artifacts"


def test_artifacts_spread_over_parts_are_completed_by_the_last_part(
    test_db, tmp_path, monkeypatch
) -> None:
    """A notebook's artifact files span the export's parts, and the sync has one part on
    disk at a time: the kept index carries what the first part held (#217)."""
    deck = {"title": "Deck", "type": "ARTIFACT_TYPE_SLIDES"}
    audio = {"title": "Talk", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW"}
    service = FakeDrive(
        [_file("p1", "takeout-9-001.zip"), _file("p2", "takeout-9-002.zip")],
        {
            "p1": _zip(
                tmp_path / "one.zip",
                {
                    **NOTEBOOK,
                    f"{ART}/Deck metadata.json": json.dumps(deck),
                    f"{ART}/Deck/Deck_-_Slide_1": "slide one",
                    f"{ART}/Talk.wav": "RIFF talk",  # its description is in part two
                },
            ),
            "p2": _zip(
                tmp_path / "two.zip",
                {
                    f"{ART}/Deck/Deck_-_Slide_2": "slide two",
                    f"{ART}/Talk metadata.json": json.dumps(audio),
                },
            ),
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
    _sync()

    def artifacts() -> dict[str, tuple]:
        with get_connection(test_db) as conn:
            rows = conn.execute(
                "SELECT i.title, p.source_id, count(m.id) FROM items i "
                "LEFT JOIN items p ON p.id = i.parent_id "
                "LEFT JOIN item_media m ON m.item_id = i.id "
                "WHERE json_extract(i.metadata, '$.type') = 'artifact' GROUP BY i.id"
            ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    assert artifacts() == {"Deck": (NB, 2), "Talk": (NB, 1)}
    state = _state(test_db)
    assert set(state["notebooklm:takeout-9"]["files"]) == {
        f"{ART}/Deck/Deck_-_Slide_1",
        f"{ART}/Deck/Deck_-_Slide_2",
        f"{ART}/Talk.wav",
    }
    assert state["p1"]["status"] == state["p2"]["status"] == "imported"
    # The next sync downloads nothing and changes nothing
    service.downloaded.clear()
    _sync()
    assert service.downloaded == [] and artifacts() == {"Deck": (NB, 2), "Talk": (NB, 1)}
    # A later sync of a part alone (forgotten) still has the other part's files
    source.forget("p2")
    _sync()
    assert artifacts() == {"Deck": (NB, 2), "Talk": (NB, 1)}

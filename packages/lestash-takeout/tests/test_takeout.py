"""Tests for the Takeout plugin and the NotebookLM changes it relies on."""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from lestash_server.routes.imports import parse_zip
from lestash_takeout import drive, source


def _notebook_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


NB = "Takeout/NotebookLM/Planning"
PART1 = {
    f"{NB}/Planning.json": json.dumps(
        {"title": "Planning", "metadata": {"createTime": "2026-03-01T10:00:00Z"}}
    ),
    f"{NB}/Sources/paper metadata.json": json.dumps({"title": "HTN paper"}),
    f"{NB}/Sources/paper.html": "<p>text</p>",
    f"{NB}/Artifacts/Deep dive.wav": "x",
    f"{NB}/Artifacts/Deep dive metadata.json": json.dumps(
        {"title": "Deep dive", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW"}
    ),
    f"{NB}/Artifacts/Briefing.json": json.dumps(
        {"title": "Briefing", "type": "ARTIFACT_TYPE_REPORT"}
    ),
    f"{NB}/Artifacts/Briefing.md": "# Briefing\nHTN planning in one page.",
}
PART2 = {f"{NB}/Artifacts/Slides.pptx": "x"}


def test_sources_only_notebook_is_kept_with_artifacts(tmp_path) -> None:
    items, source_type = parse_zip(_notebook_zip(tmp_path / "p1.zip", PART1))
    assert source_type == "notebooklm"
    parent, report = items
    assert parent.title == "Planning"
    assert parent.metadata["sources"] == ["HTN paper"]
    assert parent.metadata["artifacts"] == ["Briefing (report)", "Deep dive (audio overview)"]
    assert "Generated (2)" in parent.content
    # Text reports become searchable children of the notebook
    assert report.title == "Planning — Briefing"
    assert "HTN planning in one page." in report.content
    assert report.metadata["_parent_source_id"] == NB


def test_artifact_only_fragment_does_not_overwrite_parent(tmp_path) -> None:
    items, _ = parse_zip(_notebook_zip(tmp_path / "p2.zip", PART2))
    assert items == []


def test_parse_zip_still_accepts_bytes(tmp_path) -> None:
    data = _notebook_zip(tmp_path / "p1.zip", PART1).read_bytes()
    items, _ = parse_zip(data)
    assert items[0].title == "Planning"


class FakeDrive:
    """Just enough of the Drive v3 client for the plugin."""

    def __init__(self, files: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        self._files, self.blobs, self.downloads = files, blobs, []

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


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(drive, "get_state_path", lambda: tmp_path / "takeout_state.json")
    return tmp_path / "takeout_state.json"


def test_sync_imports_new_zips_once_and_deletes_downloads(tmp_path, state, monkeypatch) -> None:
    good = _notebook_zip(tmp_path / "good.zip", PART1)
    junk = _notebook_zip(tmp_path / "junk.zip", {"Takeout/Photos/a.jpg": "x"})
    files = [
        {"id": "f1", "name": "takeout-1-001.zip", "size": "10", "createdTime": "2026-03-01"},
        {"id": "f2", "name": "takeout-1-002.zip", "size": "10", "createdTime": "2026-03-01"},
        {"id": "f3", "name": "takeout-1.tgz", "size": "10", "createdTime": "2026-03-01"},
    ]
    service = FakeDrive(files, {"f1": good, "f2": junk})
    downloaded: list[Path] = []

    def fake_download(svc, file_id, dest):
        dest.write_bytes(svc.blobs[file_id].read_bytes())
        downloaded.append(dest)
        return dest

    monkeypatch.setattr(source, "download", fake_download)
    monkeypatch.setattr(source, "_drive_service", lambda: service)

    items = list(source.TakeoutSource().sync({}))
    assert [i.title for i in items] == ["Planning", "Planning — Briefing"]
    assert "takeout-" in service.query
    # .tgz ignored; both zips downloaded and their temp copies removed
    assert [p.name for p in downloaded] == ["takeout-1-001.zip", "takeout-1-002.zip"]
    assert not any(p.exists() for p in downloaded)

    imported = json.loads(state.read_text())["imported"]
    assert imported["f1"]["items"] == 2 and imported["f1"]["source_type"] == "notebooklm"
    assert imported["f2"]["source_type"] == "unrecognized"

    # Second sync: nothing new
    downloaded.clear()
    assert list(source.TakeoutSource().sync({})) == []
    assert downloaded == []

    # Forget one: it's imported again
    assert drive.forget("f1")
    assert [i.title for i in source.TakeoutSource().sync({})][0] == "Planning"


def test_interrupted_sync_is_retried(tmp_path, state, monkeypatch) -> None:
    good = _notebook_zip(tmp_path / "good.zip", PART1)
    files = [{"id": "f1", "name": "takeout-1-001.zip", "createdTime": "2026-03-01"}]
    service = FakeDrive(files, {"f1": good})
    monkeypatch.setattr(
        source, "download", lambda svc, fid, dest: dest.write_bytes(good.read_bytes()) and dest
    )
    monkeypatch.setattr(source, "_drive_service", lambda: service)

    gen = source.TakeoutSource().sync({})
    next(gen)  # consumer stops part-way
    gen.close()
    assert drive.load_state()["imported"] == {}


def test_no_google_signin_yields_nothing(monkeypatch) -> None:
    def no_creds():
        raise ValueError("No valid Google credentials")

    monkeypatch.setattr(source, "_drive_service", no_creds)
    assert list(source.TakeoutSource().sync({})) == []


def test_youtube_history_in_a_multi_product_takeout(tmp_path) -> None:
    yt = "Takeout/YouTube and YouTube Music"
    watch = [
        {
            "header": "YouTube",
            "title": "Watched Markov blankets explained",
            "titleUrl": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
            "time": "2026-01-01T10:00:00Z",
        }
    ]
    files = {
        **PART1,
        f"{yt}/history/watch-history.json": json.dumps(watch),
        f"{yt}/history/search-history.json": json.dumps(
            [
                {
                    "title": "Searched for markov blanket",
                    "time": "2026-01-01T09:00:00Z",
                    "titleUrl": "https://www.youtube.com/results?search_query=markov+blanket",
                }
            ]
        ),
        # A supervised child account's history must not be imported
        f"{yt}/children/Kid/watch-history.json": json.dumps(
            [{**watch[0], "titleUrl": "https://www.youtube.com/watch?v=KKKKKKKKKKK"}]
        ),
        f"{yt}/videos/my upload.mp4": "x",
    }
    items, source_type = parse_zip(_notebook_zip(tmp_path / "multi.zip", files))
    assert source_type == "takeout"  # NotebookLM + YouTube
    ids = {i.source_id for i in items}
    assert {"history:AAAAAAAAAAA", "search:markov blanket"} <= ids
    assert "history:KKKKKKKKKKK" not in ids
    assert NB in ids  # the notebook is still imported alongside


def test_youtube_only_takeout(tmp_path) -> None:
    files = {
        "Takeout/YouTube and YouTube Music/history/watch-history.json": json.dumps(
            [
                {
                    "title": "Watched X",
                    "titleUrl": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                    "time": "2026-01-01T10:00:00Z",
                }
            ]
        )
    }
    items, source_type = parse_zip(_notebook_zip(tmp_path / "yt.zip", files))
    assert source_type == "youtube"
    assert [i.source_id for i in items] == ["history:AAAAAAAAAAA"]

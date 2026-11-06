"""`lestash youtube sync` over a Takeout history item keeps its watch times."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.importers.youtube_history import parse_watch_history
from lestash_youtube.source import YouTubeSource
from typer.testing import CliRunner

VID = "dQw4w9WgXcQ"


@pytest.fixture
def test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "test.db")))
        init_database(config)
        monkeypatch.setattr(Config, "load", classmethod(lambda cls: config))
        yield config


def _takeout_entry(time: str) -> dict:
    return {
        "header": "YouTube",
        "title": "Watched Test Video Title",
        "titleUrl": f"https://www.youtube.com/watch?v={VID}",
        "subtitles": [{"name": "Test Channel", "url": "https://www.youtube.com/channel/UC1"}],
        "time": time,
    }


def test_api_history_sync_keeps_takeout_watch_times(test_db, youtube_video_factory) -> None:
    (takeout,) = parse_watch_history(
        [_takeout_entry("2026-01-01T10:00:00Z"), _takeout_entry("2026-02-01T10:00:00Z")]
    )
    with get_connection(test_db) as conn:
        upsert_item(conn, takeout)

    video = youtube_video_factory(video_id=VID)
    with (
        patch("lestash_youtube.source.load_credentials", return_value=object()),
        patch("lestash_youtube.source.create_youtube_client"),
        patch("lestash_youtube.source.get_watch_history", return_value=[video]),
    ):
        result = CliRunner().invoke(YouTubeSource().get_commands(), ["sync", "--no-likes"])
    assert result.exit_code == 0, result.output

    with get_connection(test_db) as conn:
        md = json.loads(
            conn.execute(
                "SELECT metadata FROM items WHERE source_id = ?", (f"history:{VID}",)
            ).fetchone()[0]
        )
    assert len(md["watched_times"]) == 2
    assert md["description"] == "Test video description"  # the API's field is kept too

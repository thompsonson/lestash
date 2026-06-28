"""Tests for YouTube transcript parent resolution."""

import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_youtube.source import (
    resolve_transcript_parent,
    resolve_youtube_transcript_parents,
    transcript_to_item,
)

VID = "8vHKCrNGPhY"
TRANSCRIPT = {"full_text": "hello world", "segments": [{}], "language": "en"}


@pytest.fixture
def test_db():
    """Temporary initialized database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "test.db")))
        init_database(config)
        yield config


def _video_item(subtype: str, video_id: str = VID) -> ItemCreate:
    return ItemCreate(
        source_type="youtube",
        source_id=f"{subtype}:{video_id}",
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"{subtype} video",
        content="",
    )


def _share_item(video_id: str = VID) -> ItemCreate:
    return ItemCreate(
        source_type="share",
        source_id="share-123",
        url=f"https://youtu.be/{video_id}?is=abc",
        title="Shared video",
        content="",
        metadata={"shared_text": f"https://youtu.be/{video_id}"},
    )


class TestResolveTranscriptParent:
    def test_matches_liked_video(self, test_db):
        with get_connection(test_db) as conn:
            vid_id = upsert_item(conn, _video_item("liked"))
            assert resolve_transcript_parent(conn, VID) == vid_id

    def test_matches_history_video(self, test_db):
        with get_connection(test_db) as conn:
            vid_id = upsert_item(conn, _video_item("history"))
            assert resolve_transcript_parent(conn, VID) == vid_id

    def test_falls_back_to_share_item(self, test_db):
        with get_connection(test_db) as conn:
            share_id = upsert_item(conn, _share_item())
            assert resolve_transcript_parent(conn, VID) == share_id

    def test_prefers_youtube_item_over_share(self, test_db):
        with get_connection(test_db) as conn:
            upsert_item(conn, _share_item())
            vid_id = upsert_item(conn, _video_item("liked"))
            assert resolve_transcript_parent(conn, VID) == vid_id

    def test_returns_none_when_no_match(self, test_db):
        with get_connection(test_db) as conn:
            assert resolve_transcript_parent(conn, VID) is None

    def test_ignores_other_videos(self, test_db):
        with get_connection(test_db) as conn:
            upsert_item(conn, _video_item("liked", video_id="OTHERvideoX"))
            assert resolve_transcript_parent(conn, VID) is None

    def test_does_not_match_transcript_itself(self, test_db):
        """A transcript's own item (url contains video_id) must not be its parent."""
        with get_connection(test_db) as conn:
            t_id = upsert_item(conn, transcript_to_item(VID, TRANSCRIPT))
            assert resolve_transcript_parent(conn, VID) != t_id
            assert resolve_transcript_parent(conn, VID) is None


class TestResolveYoutubeTranscriptParents:
    def test_backfills_orphan_transcript(self, test_db):
        with get_connection(test_db) as conn:
            vid_id = upsert_item(conn, _video_item("liked"))
            t_id = upsert_item(conn, transcript_to_item(VID, TRANSCRIPT))

            resolved = resolve_youtube_transcript_parents(conn)

            assert resolved == 1
            parent = conn.execute("SELECT parent_id FROM items WHERE id = ?", (t_id,)).fetchone()[0]
            assert parent == vid_id

    def test_backfills_transcript_under_share(self, test_db):
        with get_connection(test_db) as conn:
            share_id = upsert_item(conn, _share_item())
            t_id = upsert_item(conn, transcript_to_item(VID, TRANSCRIPT))

            assert resolve_youtube_transcript_parents(conn) == 1
            parent = conn.execute("SELECT parent_id FROM items WHERE id = ?", (t_id,)).fetchone()[0]
            assert parent == share_id

    def test_noop_when_no_parent_exists(self, test_db):
        with get_connection(test_db) as conn:
            upsert_item(conn, transcript_to_item(VID, TRANSCRIPT))
            assert resolve_youtube_transcript_parents(conn) == 0

    def test_skips_already_parented(self, test_db):
        with get_connection(test_db) as conn:
            vid_id = upsert_item(conn, _video_item("liked"))
            transcript = transcript_to_item(VID, TRANSCRIPT)
            transcript.parent_id = vid_id
            upsert_item(conn, transcript)
            assert resolve_youtube_transcript_parents(conn) == 0

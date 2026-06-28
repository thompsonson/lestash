"""Tests for the YouTube Data API client helpers."""

from unittest.mock import MagicMock

from lestash_youtube.client import get_video_details


def _client_returning(payload: dict) -> MagicMock:
    youtube = MagicMock()
    youtube.videos.return_value.list.return_value.execute.return_value = payload
    return youtube


class TestGetVideoDetails:
    def test_shapes_response_for_video_to_item(self):
        youtube = _client_returning(
            {
                "items": [
                    {
                        "snippet": {
                            "title": "My Title",
                            "description": "My description",
                            "channelId": "UC_chan",
                            "channelTitle": "My Channel",
                            "publishedAt": "2025-01-01T00:00:00Z",
                            "thumbnails": {"high": {"url": "https://t/hq.jpg"}},
                            "tags": ["a", "b"],
                            "categoryId": "22",
                        },
                        "contentDetails": {"duration": "PT4M35S", "definition": "hd"},
                        "statistics": {
                            "viewCount": "100",
                            "likeCount": "5",
                            "commentCount": "2",
                        },
                    }
                ]
            }
        )

        details = get_video_details(youtube, "dQw4w9WgXcQ")

        assert details == {
            "id": "dQw4w9WgXcQ",
            "title": "My Title",
            "description": "My description",
            "channel_id": "UC_chan",
            "channel_title": "My Channel",
            "published_at": "2025-01-01T00:00:00Z",
            "thumbnails": {"high": {"url": "https://t/hq.jpg"}},
            "tags": ["a", "b"],
            "category_id": "22",
            "duration": "PT4M35S",
            "definition": "hd",
            "view_count": "100",
            "like_count": "5",
            "comment_count": "2",
        }
        youtube.videos.return_value.list.assert_called_once_with(
            part="snippet,contentDetails,statistics", id="dQw4w9WgXcQ"
        )

    def test_returns_none_when_video_missing(self):
        youtube = _client_returning({"items": []})
        assert get_video_details(youtube, "dQw4w9WgXcQ") is None

"""Tests for YouTube CLI commands."""

from unittest.mock import MagicMock, patch

import pytest
from lestash_youtube.source import YouTubeSource
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def youtube_app():
    """Get the YouTube CLI app."""
    source = YouTubeSource()
    return source.get_commands()


class TestAuthCommand:
    """Test the auth command."""

    def test_auth_fails_without_client_secrets(self, youtube_app):
        """Should fail with helpful message when client secrets not found."""
        with patch("lestash_youtube.source.check_client_secrets", return_value=False):
            result = runner.invoke(youtube_app, ["auth"])

        assert result.exit_code == 1
        assert "OAuth client secrets not found" in result.output
        assert "console.cloud.google.com" in result.output

    def test_auth_runs_oauth_flow(self, youtube_app):
        """Should run OAuth flow when client secrets exist."""
        mock_creds = MagicMock()
        mock_channel = {"title": "Test Channel", "custom_url": "@testchannel"}

        with (
            patch("lestash_youtube.source.check_client_secrets", return_value=True),
            patch("lestash_youtube.source.run_oauth_flow", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_channel_info", return_value=mock_channel),
        ):
            result = runner.invoke(youtube_app, ["auth"])

        assert result.exit_code == 0
        assert "Authentication successful" in result.output
        assert "Test Channel" in result.output


class TestStatusCommand:
    """Test the status command."""

    def test_status_fails_without_client_secrets(self, youtube_app):
        """Should fail when client secrets not found."""
        with patch("lestash_youtube.source.check_client_secrets", return_value=False):
            result = runner.invoke(youtube_app, ["status"])

        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_status_fails_without_credentials(self, youtube_app):
        """Should fail when not authenticated."""
        with (
            patch("lestash_youtube.source.check_client_secrets", return_value=True),
            patch("lestash_youtube.source.load_credentials", return_value=None),
        ):
            result = runner.invoke(youtube_app, ["status"])

        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_status_shows_channel_info(self, youtube_app):
        """Should display channel information when authenticated."""
        mock_creds = MagicMock()
        mock_channel = {
            "title": "My Channel",
            "custom_url": "@mychannel",
            "subscriber_count": "1000",
            "video_count": "50",
            "view_count": "100000",
        }

        with (
            patch("lestash_youtube.source.check_client_secrets", return_value=True),
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_channel_info", return_value=mock_channel),
        ):
            result = runner.invoke(youtube_app, ["status"])

        assert result.exit_code == 0
        assert "Connected" in result.output
        assert "My Channel" in result.output


class TestSyncCommand:
    """Test the sync command."""

    def test_sync_fails_without_credentials(self, youtube_app):
        """Should fail when not authenticated."""
        with patch("lestash_youtube.source.load_credentials", return_value=None):
            result = runner.invoke(youtube_app, ["sync"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_sync_fetches_liked_videos(self, youtube_app):
        """Should sync liked videos by default."""
        mock_creds = MagicMock()
        mock_videos = [
            {
                "id": "vid1",
                "title": "Video 1",
                "description": "Desc 1",
                "channel_id": "ch1",
                "channel_title": "Channel 1",
                "published_at": "2025-01-15T10:00:00Z",
                "duration": "PT5M",
                "definition": "hd",
                "view_count": "1000",
                "like_count": "100",
                "comment_count": "10",
                "tags": [],
                "category_id": "22",
                "thumbnails": {},
            }
        ]

        # Create mock connection context manager
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor

        with (
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_liked_videos", return_value=mock_videos),
            patch("lestash_youtube.source.get_watch_history", return_value=[]),
            patch("lestash.core.config.Config.load") as mock_config,
            patch("lestash.core.database.get_connection") as mock_get_conn,
        ):
            mock_config.return_value = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=None)

            result = runner.invoke(youtube_app, ["sync"])

        assert result.exit_code == 0
        assert "liked videos" in result.output.lower()

    def test_sync_shows_history_warning(self, youtube_app):
        """Should show warning when history is empty."""
        mock_creds = MagicMock()
        mock_conn = MagicMock()

        with (
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_liked_videos", return_value=[]),
            patch("lestash_youtube.source.get_watch_history", return_value=[]),
            patch("lestash.core.config.Config.load") as mock_config,
            patch("lestash.core.database.get_connection") as mock_get_conn,
        ):
            mock_config.return_value = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=None)

            result = runner.invoke(youtube_app, ["sync"])

        assert "restricted" in result.output.lower() or "empty" in result.output.lower()

    def test_sync_respects_no_history_flag(self, youtube_app):
        """Should skip history sync when --no-history is passed."""
        mock_creds = MagicMock()
        mock_conn = MagicMock()

        with (
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_liked_videos", return_value=[]),
            patch("lestash_youtube.source.get_watch_history") as mock_history,
            patch("lestash.core.config.Config.load") as mock_config,
            patch("lestash.core.database.get_connection") as mock_get_conn,
        ):
            mock_config.return_value = MagicMock()
            mock_get_conn.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_get_conn.return_value.__exit__ = MagicMock(return_value=None)

            runner.invoke(youtube_app, ["sync", "--no-history"])

        # get_watch_history should not be called
        mock_history.assert_not_called()


class TestLikesCommand:
    """Test the likes preview command."""

    def test_likes_fails_without_credentials(self, youtube_app):
        """Should fail when not authenticated."""
        with patch("lestash_youtube.source.load_credentials", return_value=None):
            result = runner.invoke(youtube_app, ["likes"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_likes_displays_video_table(self, youtube_app):
        """Should display liked videos in a table."""
        mock_creds = MagicMock()
        mock_videos = [
            {
                "id": "vid1",
                "title": "Great Video Title",
                "channel_title": "Awesome Channel",
                "duration": "PT10M30S",
                "published_at": "2025-01-15T10:00:00Z",
            }
        ]

        with (
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_liked_videos", return_value=mock_videos),
        ):
            result = runner.invoke(youtube_app, ["likes"])

        assert result.exit_code == 0
        assert "Great Video Title" in result.output
        assert "Awesome Channel" in result.output


class TestHistoryCommand:
    """Test the history preview command."""

    def test_history_fails_without_credentials(self, youtube_app):
        """Should fail when not authenticated."""
        with patch("lestash_youtube.source.load_credentials", return_value=None):
            result = runner.invoke(youtube_app, ["history"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.output

    def test_history_shows_takeout_instructions_when_empty(self, youtube_app):
        """Should show Takeout instructions when history is empty."""
        mock_creds = MagicMock()

        with (
            patch("lestash_youtube.source.load_credentials", return_value=mock_creds),
            patch("lestash_youtube.source.create_youtube_client"),
            patch("lestash_youtube.source.get_watch_history", return_value=[]),
        ):
            result = runner.invoke(youtube_app, ["history"])

        assert result.exit_code == 0
        assert "takeout.google.com" in result.output.lower()


class TestYouTubeSourcePlugin:
    """Test YouTubeSource plugin class."""

    def test_has_correct_name(self):
        """Should have 'youtube' as name."""
        source = YouTubeSource()
        assert source.name == "youtube"

    def test_has_description(self):
        """Should have a description."""
        source = YouTubeSource()
        assert source.description is not None
        assert len(source.description) > 0

    def test_returns_typer_app(self):
        """Should return a Typer app from get_commands."""
        import typer

        source = YouTubeSource()
        app = source.get_commands()
        assert isinstance(app, typer.Typer)

    def test_configure_returns_defaults(self):
        """Should return default configuration."""
        source = YouTubeSource()
        config = source.configure()

        assert isinstance(config, dict)
        assert "sync_likes" in config
        assert "sync_history" in config
        assert config["sync_likes"] is True
        assert config["sync_history"] is True

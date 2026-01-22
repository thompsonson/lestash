"""Tests for Bluesky CLI commands - Sprint 3."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from lestash_bluesky.source import BlueskySource
from typer.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Create a Typer CLI runner."""
    return CliRunner()


@pytest.fixture
def bluesky_app():
    """Get the Bluesky CLI app."""
    source = BlueskySource()
    return source.get_commands()


class TestAuthCommand:
    """Test the auth command."""

    def test_saves_credentials_successfully(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should save credentials when authentication succeeds."""
        # Mock paths
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        # Mock client
        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_client.export_session_string.return_value = "session-string"

        # Patch create_client directly instead of Client
        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(
                bluesky_app, ["auth", "--handle", "alice.bsky.social", "--password", "pass123"]
            )

        assert result.exit_code == 0
        assert "Authenticated as alice.bsky.social" in result.stdout
        assert creds_file.exists()

        # Verify credentials saved
        creds_data = json.loads(creds_file.read_text())
        assert creds_data["handle"] == "alice.bsky.social"

    def test_prompts_for_missing_handle(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should prompt for handle when not provided."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="bob.bsky.social", did="did:plc:bob123")
        mock_client.export_session_string.return_value = "session"

        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(
                bluesky_app,
                ["auth", "--password", "pass123"],
                input="bob.bsky.social\n",
            )

        assert result.exit_code == 0
        assert "Bluesky handle" in result.stdout

    def test_prompts_for_missing_password(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should prompt for password when not provided."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_client.export_session_string.return_value = "session"

        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(
                bluesky_app,
                ["auth", "--handle", "alice.bsky.social"],
                input="password123\n",
            )

        assert result.exit_code == 0
        assert "Password" in result.stdout

    def test_shows_success_message(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should show success message with account info."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_client.export_session_string.return_value = "session"

        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(
                bluesky_app, ["auth", "--handle", "alice.bsky.social", "--password", "pass"]
            )

        assert result.exit_code == 0
        assert "Authenticated as alice.bsky.social" in result.stdout
        assert "did:plc:abc123" in result.stdout
        assert "Credentials saved" in result.stdout

    def test_handles_authentication_failure(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should show error message when authentication fails."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        with patch(
            "lestash_bluesky.client.create_client",
            side_effect=Exception("Invalid credentials"),
        ):
            result = cli_runner.invoke(
                bluesky_app,
                ["auth", "--handle", "alice.bsky.social", "--password", "wrong"],
            )

        assert result.exit_code == 1
        assert "Authentication failed" in result.stdout


class TestSyncCommand:
    """Test the sync command."""

    def test_syncs_posts_successfully(
        self, cli_runner, bluesky_app, tmp_path, monkeypatch, bluesky_post_factory
    ):
        """Should sync posts and save to database."""
        # Setup credentials
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        # Mock client and posts
        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social")

        mock_posts = [
            bluesky_post_factory(text="Post 1"),
            bluesky_post_factory(text="Post 2"),
        ]

        # Mock database
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)

        mock_config = MagicMock()

        with (
            patch("lestash_bluesky.client.create_client", return_value=mock_client),
            patch("lestash_bluesky.client.get_author_posts", return_value=mock_posts),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(bluesky_app, ["sync"])

        assert result.exit_code == 0
        assert "Found 2 posts" in result.stdout
        assert "Synced" in result.stdout

    def test_shows_progress_and_count(
        self, cli_runner, bluesky_app, tmp_path, monkeypatch, bluesky_post_factory
    ):
        """Should display progress information during sync."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_posts = [bluesky_post_factory(text=f"Post {i}") for i in range(5)]

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_bluesky.client.create_client", return_value=mock_client),
            patch("lestash_bluesky.client.get_author_posts", return_value=mock_posts),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(bluesky_app, ["sync"])

        assert "Found 5 posts" in result.stdout
        assert "Synced" in result.stdout

    def test_handles_authentication_error(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should show error when not authenticated."""
        creds_file = tmp_path / "credentials.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)

        result = cli_runner.invoke(bluesky_app, ["sync"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout

    def test_handles_network_error(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should handle network errors gracefully."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        with patch("lestash_bluesky.client.create_client", side_effect=Exception("Network error")):
            result = cli_runner.invoke(bluesky_app, ["sync"])

        assert result.exit_code == 1
        assert "Sync failed" in result.stdout

    def test_respects_limit_parameter(
        self, cli_runner, bluesky_app, tmp_path, monkeypatch, bluesky_post_factory
    ):
        """Should pass limit parameter to get_author_posts."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_posts = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_bluesky.client.create_client", return_value=mock_client),
            patch(
                "lestash_bluesky.client.get_author_posts", return_value=mock_posts
            ) as mock_get_posts,
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(bluesky_app, ["sync", "--limit", "50"])

        assert result.exit_code == 0
        mock_get_posts.assert_called_once()
        assert mock_get_posts.call_args[1]["limit"] == 50

    def test_handles_empty_timeline(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should handle empty timeline gracefully."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_posts = []

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_bluesky.client.create_client", return_value=mock_client),
            patch("lestash_bluesky.client.get_author_posts", return_value=mock_posts),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(bluesky_app, ["sync"])

        assert result.exit_code == 0
        assert "Found 0 posts" in result.stdout

    def test_deduplicates_existing_posts(
        self, cli_runner, bluesky_app, tmp_path, monkeypatch, bluesky_post_factory
    ):
        """Should not duplicate posts that already exist."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_posts = [bluesky_post_factory(text="Duplicate post")]

        # Mock cursor to show 0 rows affected (duplicate)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0  # No rows inserted (duplicate)
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_bluesky.client.create_client", return_value=mock_client),
            patch("lestash_bluesky.client.get_author_posts", return_value=mock_posts),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(bluesky_app, ["sync"])

        assert result.exit_code == 0
        assert "Synced 0 posts" in result.stdout  # 0 new posts


class TestStatusCommand:
    """Test the status command."""

    def test_shows_authenticated_user_info(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should display authenticated user information."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_profile = SimpleNamespace(
            posts_count=100, followers_count=50, follows_count=75, display_name="Alice"
        )
        mock_client.app.bsky.actor.get_profile.return_value = mock_profile

        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(bluesky_app, ["status"])

        assert result.exit_code == 0
        assert "Bluesky Status" in result.stdout
        assert "alice.bsky.social" in result.stdout
        assert "did:plc:abc123" in result.stdout

    def test_shows_unauthenticated_status(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should show unauthenticated message when no credentials."""
        creds_file = tmp_path / "credentials.json"
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)

        result = cli_runner.invoke(bluesky_app, ["status"])

        assert result.exit_code == 1
        assert "Not found" in result.stdout
        assert "lestash bluesky auth" in result.stdout

    def test_shows_sync_statistics(self, cli_runner, bluesky_app, tmp_path, monkeypatch):
        """Should display sync statistics from database."""
        creds_file = tmp_path / "credentials.json"
        session_file = tmp_path / "session.json"
        creds_file.write_text(json.dumps({"handle": "alice.bsky.social", "password": "pass"}))
        monkeypatch.setattr("lestash_bluesky.source.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.source.get_session_path", lambda: session_file)
        monkeypatch.setattr("lestash_bluesky.client.get_credentials_path", lambda: creds_file)
        monkeypatch.setattr("lestash_bluesky.client.get_session_path", lambda: session_file)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_profile = SimpleNamespace(
            posts_count=100, followers_count=50, follows_count=75, display_name="Alice"
        )
        mock_client.app.bsky.actor.get_profile.return_value = mock_profile

        with patch("lestash_bluesky.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(bluesky_app, ["status"])

        assert result.exit_code == 0
        # Should show some profile stats
        assert "Bluesky Status" in result.stdout

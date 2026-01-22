"""Tests for Micro.blog CLI commands."""

from unittest.mock import MagicMock, Mock, patch

import pytest
from lestash_microblog.source import MicroblogSource
from typer.testing import CliRunner


@pytest.fixture
def cli_runner():
    """Create a Typer CLI runner."""
    return CliRunner()


@pytest.fixture
def microblog_app():
    """Get the Micro.blog CLI app."""
    source = MicroblogSource()
    return source.get_commands()


class TestAuthCommand:
    """Test the auth command."""

    def test_saves_token_successfully(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should save token when verification succeeds."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = {"destination": []}
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["auth", "--token", "test-token-12345"])

        assert result.exit_code == 0
        assert "verified and saved" in result.stdout
        assert token_file.exists()

    def test_prompts_for_missing_token(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should prompt for token when not provided."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = {"destination": []}
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["auth"], input="prompted-token\n")

        assert result.exit_code == 0
        assert "API token" in result.stdout

    def test_shows_available_destinations(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should display available blogs after auth."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = {
            "destination": [
                {"uid": "https://blog.example.com/", "name": "My Blog"},
            ]
        }
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["auth", "--token", "test"])

        assert result.exit_code == 0
        assert "My Blog" in result.stdout

    def test_handles_invalid_token(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should show error for invalid token."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.side_effect = Exception("401 Unauthorized")
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["auth", "--token", "bad-token"])

        assert result.exit_code == 1
        assert "Authentication failed" in result.stdout


class TestLogoutCommand:
    """Test the logout command."""

    def test_removes_token(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should remove saved token."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        result = cli_runner.invoke(microblog_app, ["logout"])

        assert result.exit_code == 0
        assert "removed" in result.stdout
        assert not token_file.exists()

    def test_handles_no_token(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should handle case when no token exists."""
        monkeypatch.setattr(
            "lestash_microblog.source.get_token_path", lambda: tmp_path / "nonexistent.json"
        )
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path", lambda: tmp_path / "nonexistent.json"
        )

        result = cli_runner.invoke(microblog_app, ["logout"])

        assert result.exit_code == 0
        assert "No token found" in result.stdout


class TestSyncCommand:
    """Test the sync command."""

    def test_syncs_posts_successfully(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_posts
    ):
        """Should sync posts and save to database."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test-token"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = mock_micropub_posts
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(microblog_app, ["sync"])

        assert result.exit_code == 0
        assert "Found 3 posts" in result.stdout
        assert "Synced" in result.stdout

    def test_shows_authentication_error(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should show error when not authenticated."""
        monkeypatch.setattr(
            "lestash_microblog.source.get_token_path", lambda: tmp_path / "nonexistent.json"
        )
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path", lambda: tmp_path / "nonexistent.json"
        )

        result = cli_runner.invoke(microblog_app, ["sync"])

        assert result.exit_code == 1
        assert "Not authenticated" in result.stdout

    def test_handles_network_error(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should handle network errors gracefully."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test-token"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.side_effect = Exception("Network error")
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["sync"])

        assert result.exit_code == 1
        assert "Sync failed" in result.stdout

    def test_respects_limit_parameter(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_posts
    ):
        """Should pass limit parameter to client."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = []
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(microblog_app, ["sync", "--limit", "50"])

        assert result.exit_code == 0
        mock_client.get_all_posts.assert_called_once()
        call_kwargs = mock_client.get_all_posts.call_args[1]
        assert call_kwargs["limit"] == 50

    def test_respects_max_parameter(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_posts
    ):
        """Should pass max_posts parameter to client."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = []
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(microblog_app, ["sync", "--max", "25"])

        assert result.exit_code == 0
        call_kwargs = mock_client.get_all_posts.call_args[1]
        assert call_kwargs["max_posts"] == 25

    def test_respects_destination_parameter(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_posts
    ):
        """Should pass destination parameter to client."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = []
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(
                microblog_app, ["sync", "--destination", "https://blog.example.com/"]
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.get_all_posts.call_args[1]
        assert call_kwargs["destination"] == "https://blog.example.com/"

    def test_handles_empty_posts(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should handle empty posts gracefully."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = []
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            result = cli_runner.invoke(microblog_app, ["sync"])

        assert result.exit_code == 0
        assert "Found 0 posts" in result.stdout


class TestStatusCommand:
    """Test the status command."""

    def test_shows_authenticated_status(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_config
    ):
        """Should display authenticated status."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test-token-12345678"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = mock_micropub_config
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["status"])

        assert result.exit_code == 0
        assert "Micro.blog Status" in result.stdout
        assert "Found" in result.stdout
        assert "Connected" in result.stdout

    def test_shows_unauthenticated_status(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should show unauthenticated message when no token."""
        monkeypatch.setattr(
            "lestash_microblog.source.get_token_path", lambda: tmp_path / "nonexistent.json"
        )
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path", lambda: tmp_path / "nonexistent.json"
        )

        result = cli_runner.invoke(microblog_app, ["status"])

        assert result.exit_code == 1
        assert "Not found" in result.stdout
        assert "lestash microblog auth" in result.stdout

    def test_shows_available_blogs(
        self, cli_runner, microblog_app, tmp_path, monkeypatch, mock_micropub_config
    ):
        """Should display available blogs."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test-token-12345678"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = mock_micropub_config
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["status"])

        assert result.exit_code == 0
        assert "Main Blog" in result.stdout
        assert "Photos" in result.stdout

    def test_handles_connection_failure(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should handle connection failure gracefully."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.side_effect = Exception("Connection failed")
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["status"])

        assert result.exit_code == 1
        assert "Failed" in result.stdout

    def test_masks_token_in_display(self, cli_runner, microblog_app, tmp_path, monkeypatch):
        """Should mask token in output."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "verysecrettoken123"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.verify_token.return_value = {"destination": []}
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            result = cli_runner.invoke(microblog_app, ["status"])

        assert result.exit_code == 0
        # Should not show full token
        assert "verysecrettoken123" not in result.stdout
        # Should show masked version
        assert "..." in result.stdout


class TestMicroblogSourcePlugin:
    """Test MicroblogSource plugin class."""

    def test_plugin_name(self):
        """Should have correct name."""
        source = MicroblogSource()
        assert source.name == "microblog"

    def test_plugin_description(self):
        """Should have description."""
        source = MicroblogSource()
        assert "Micro.blog" in source.description

    def test_get_commands_returns_typer_app(self):
        """Should return Typer app."""
        import typer

        source = MicroblogSource()
        app = source.get_commands()

        assert isinstance(app, typer.Typer)

    def test_configure_returns_defaults(self):
        """Should return default configuration."""
        source = MicroblogSource()
        config = source.configure()

        assert "destination" in config
        assert "limit" in config

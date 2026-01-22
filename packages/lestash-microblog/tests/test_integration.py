"""Integration tests for Micro.blog plugin."""

from unittest.mock import MagicMock, Mock, patch

from lestash_microblog.source import MicroblogSource


class TestSyncMethod:
    """Test the sync generator method."""

    def test_sync_yields_items(self, tmp_path, monkeypatch, mock_micropub_posts):
        """Should yield ItemCreate objects."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = mock_micropub_posts
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            source = MicroblogSource()
            items = list(source.sync({}))

        assert len(items) == 3
        assert all(item.source_type == "microblog" for item in items)

    def test_sync_passes_config_to_client(self, tmp_path, monkeypatch):
        """Should pass config parameters to client."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = []
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            source = MicroblogSource()
            config = {
                "limit": 50,
                "destination": "https://blog.example.com/",
                "max_posts": 100,
            }
            list(source.sync(config))

        call_kwargs = mock_client.get_all_posts.call_args[1]
        assert call_kwargs["limit"] == 50
        assert call_kwargs["destination"] == "https://blog.example.com/"
        assert call_kwargs["max_posts"] == 100

    def test_sync_handles_no_token(self, tmp_path, monkeypatch):
        """Should handle missing token gracefully."""
        monkeypatch.setattr(
            "lestash_microblog.source.get_token_path", lambda: tmp_path / "nonexistent.json"
        )
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path", lambda: tmp_path / "nonexistent.json"
        )

        source = MicroblogSource()
        items = list(source.sync({}))

        assert items == []

    def test_sync_handles_client_error(self, tmp_path, monkeypatch):
        """Should handle client errors gracefully."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        mock_client = MagicMock()
        mock_client.get_all_posts.side_effect = Exception("API Error")
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            source = MicroblogSource()
            items = list(source.sync({}))

        assert items == []

    def test_sync_skips_invalid_posts(self, tmp_path, monkeypatch, microblog_post_factory):
        """Should skip posts that fail to process."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        valid_post = microblog_post_factory(content="Valid post")
        # Invalid post missing required structure
        invalid_post = {"type": ["h-entry"]}  # Missing properties

        mock_client = MagicMock()
        mock_client.get_all_posts.return_value = [valid_post, invalid_post]
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with patch("lestash_microblog.client.create_client", return_value=mock_client):
            source = MicroblogSource()
            items = list(source.sync({}))

        # Should have processed at least the valid post
        assert len(items) >= 1


class TestEndToEndWorkflow:
    """Test complete workflow scenarios."""

    def test_auth_then_sync_workflow(
        self, tmp_path, monkeypatch, mock_micropub_config, mock_micropub_posts
    ):
        """Should complete auth and sync workflow."""
        from typer.testing import CliRunner

        token_file = tmp_path / "token.json"
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        # Auth step
        mock_auth_client = MagicMock()
        mock_auth_client.verify_token.return_value = mock_micropub_config
        mock_auth_client.__enter__ = Mock(return_value=mock_auth_client)
        mock_auth_client.__exit__ = Mock(return_value=False)

        source = MicroblogSource()
        app = source.get_commands()
        runner = CliRunner()

        with patch("lestash_microblog.client.create_client", return_value=mock_auth_client):
            auth_result = runner.invoke(app, ["auth", "--token", "workflow-token"])

        assert auth_result.exit_code == 0
        assert token_file.exists()

        # Sync step
        mock_sync_client = MagicMock()
        mock_sync_client.get_all_posts.return_value = mock_micropub_posts
        mock_sync_client.__enter__ = Mock(return_value=mock_sync_client)
        mock_sync_client.__exit__ = Mock(return_value=False)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        mock_config = MagicMock()

        with (
            patch("lestash_microblog.client.create_client", return_value=mock_sync_client),
            patch("lestash.core.database.get_connection", return_value=mock_conn),
            patch("lestash.core.config.Config.load", return_value=mock_config),
        ):
            sync_result = runner.invoke(app, ["sync"])

        assert sync_result.exit_code == 0
        assert "Synced" in sync_result.stdout

    def test_logout_removes_access(self, tmp_path, monkeypatch):
        """Should prevent sync after logout."""
        from typer.testing import CliRunner

        token_file = tmp_path / "token.json"
        token_file.write_text('{"token": "test"}')
        monkeypatch.setattr("lestash_microblog.source.get_token_path", lambda: token_file)
        monkeypatch.setattr("lestash_microblog.client.get_token_path", lambda: token_file)

        source = MicroblogSource()
        app = source.get_commands()
        runner = CliRunner()

        # Logout
        logout_result = runner.invoke(app, ["logout"])
        assert logout_result.exit_code == 0

        # Try to sync - should fail
        sync_result = runner.invoke(app, ["sync"])
        assert sync_result.exit_code == 1
        assert "Not authenticated" in sync_result.stdout

"""Tests for Micro.blog authentication and token management."""

import os

from lestash_microblog.client import (
    delete_token,
    load_token,
    save_token,
)


class TestTokenManagement:
    """Test token save/load/delete functionality."""

    def test_save_and_load_token(self, tmp_path, monkeypatch):
        """Should save token to file and load it back."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        save_token("test-api-token-12345")
        loaded = load_token()

        assert loaded == "test-api-token-12345"

    def test_token_file_permissions(self, tmp_path, monkeypatch):
        """Should create token file with 0600 permissions."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        save_token("secret-token")

        stat_info = os.stat(token_file)
        permissions = stat_info.st_mode & 0o777
        assert permissions == 0o600

    def test_load_token_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Should return None when token file doesn't exist."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "does_not_exist.json",
        )

        token = load_token()

        assert token is None

    def test_load_token_handles_corrupted_json(self, tmp_path, monkeypatch):
        """Should return None when token file contains invalid JSON."""
        token_file = tmp_path / "token.json"
        token_file.write_text("not valid json {{{")
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        token = load_token()

        assert token is None

    def test_load_token_handles_missing_key(self, tmp_path, monkeypatch):
        """Should return None when token key is missing from JSON."""
        token_file = tmp_path / "token.json"
        token_file.write_text('{"other_key": "value"}')
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        token = load_token()

        assert token is None

    def test_delete_token_removes_file(self, tmp_path, monkeypatch):
        """Should delete the token file."""
        token_file = tmp_path / "token.json"
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        save_token("token-to-delete")
        assert token_file.exists()

        result = delete_token()

        assert result is True
        assert not token_file.exists()

    def test_delete_token_returns_false_when_not_found(self, tmp_path, monkeypatch):
        """Should return False when no token file exists."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "nonexistent.json",
        )

        result = delete_token()

        assert result is False

    def test_save_token_overwrites_existing(self, tmp_path, monkeypatch):
        """Should overwrite existing token."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        save_token("first-token")
        save_token("second-token")
        loaded = load_token()

        assert loaded == "second-token"

    def test_token_stored_in_json_format(self, tmp_path, monkeypatch):
        """Should store token in proper JSON format."""
        import json

        token_file = tmp_path / "token.json"
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        save_token("my-token")

        data = json.loads(token_file.read_text())
        assert data == {"token": "my-token"}

"""Tests for Bluesky authentication and session management - Sprint 2."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from lestash_bluesky.client import (
    create_client,
    load_credentials,
    load_session,
    save_credentials,
    save_session,
)


@pytest.fixture
def mock_atproto_client(monkeypatch):
    """Mock the atproto Client class."""
    mock_client_class = MagicMock()

    # Ensure atproto module exists in sys.modules
    if "atproto" not in sys.modules:
        sys.modules["atproto"] = MagicMock()

    # Patch the Client class in atproto module
    # Use raising=False since Client might not exist yet
    monkeypatch.setattr(sys.modules["atproto"], "Client", mock_client_class, raising=False)

    return mock_client_class


class TestCredentialsManagement:
    """Test credentials save/load functionality."""

    def test_save_and_load_credentials(self, tmp_path, monkeypatch):
        """Should save credentials to file and load them back."""
        # Patch get_credentials_path to use tmp_path
        monkeypatch.setattr(
            "lestash_bluesky.client.get_credentials_path",
            lambda: tmp_path / "credentials.json",
        )

        # Save credentials
        save_credentials("alice.bsky.social", "test-password")

        # Load them back
        creds = load_credentials()

        assert creds is not None
        assert creds["handle"] == "alice.bsky.social"
        assert creds["password"] == "test-password"

    def test_credentials_file_permissions(self, tmp_path, monkeypatch):
        """Should create credentials file with 0600 permissions."""
        creds_file = tmp_path / "credentials.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_credentials_path",
            lambda: creds_file,
        )

        save_credentials("user.bsky.social", "secret")

        # Check file permissions (owner read/write only)
        stat_info = os.stat(creds_file)
        permissions = stat_info.st_mode & 0o777
        assert permissions == 0o600

    def test_load_credentials_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Should return None when credentials file doesn't exist."""
        monkeypatch.setattr(
            "lestash_bluesky.client.get_credentials_path",
            lambda: tmp_path / "does_not_exist.json",
        )

        creds = load_credentials()

        assert creds is None


class TestSessionManagement:
    """Test session save/load functionality."""

    def test_save_and_load_session(self, tmp_path, monkeypatch):
        """Should save session string to file and load it back."""
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )

        session_string = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        save_session(session_string)

        loaded = load_session()

        assert loaded == session_string

    def test_session_file_permissions(self, tmp_path, monkeypatch):
        """Should create session file with 0600 permissions."""
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )

        save_session("test-session-string")

        # Check file permissions
        stat_info = os.stat(session_file)
        permissions = stat_info.st_mode & 0o777
        assert permissions == 0o600

    def test_load_session_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Should return None when session file doesn't exist."""
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: tmp_path / "does_not_exist.json",
        )

        session = load_session()

        assert session is None

    def test_load_session_handles_corrupted_file(self, tmp_path, monkeypatch):
        """Should return empty string when session file is empty."""
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )

        # Write empty file
        session_file.write_text("")

        session = load_session()

        # Should read empty string successfully
        assert session == ""


class TestCreateClient:
    """Test create_client authentication logic."""

    def test_creates_client_with_full_login(
        self, mock_atproto_client, tmp_path, monkeypatch
    ):
        """Should perform full login when no session exists."""
        # Setup - no existing session
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: tmp_path / "session.json",
        )

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_client.export_session_string.return_value = "new-session-string"
        mock_atproto_client.return_value = mock_client

        # Execute
        client = create_client("alice.bsky.social", "password123")

        # Verify
        assert client == mock_client
        mock_client.login.assert_called_once_with("alice.bsky.social", "password123")
        mock_client.export_session_string.assert_called_once()

        # Session should be saved
        session_file = tmp_path / "session.json"
        assert session_file.read_text() == "new-session-string"

    def test_reuses_valid_session(self, mock_atproto_client, tmp_path, monkeypatch):
        """Should reuse existing session instead of full login."""
        # Setup - existing session
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )
        session_file.write_text("existing-session-string")
        session_file.chmod(0o600)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_atproto_client.return_value = mock_client

        # Execute
        client = create_client("alice.bsky.social", "password123")

        # Verify - should use session string, not handle/password
        assert client == mock_client
        mock_client.login.assert_called_once_with(session_string="existing-session-string")

    def test_falls_back_to_login_on_invalid_session(
        self, mock_atproto_client, tmp_path, monkeypatch
    ):
        """Should fall back to full login if session is invalid."""
        # Setup - session exists but is invalid
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )
        session_file.write_text("invalid-session")
        session_file.chmod(0o600)

        mock_client = MagicMock()
        mock_client.me = SimpleNamespace(handle="alice.bsky.social", did="did:plc:abc123")
        mock_client.export_session_string.return_value = "new-session-string"

        # First login call (with session) raises exception
        # Second login call (with handle/password) succeeds
        mock_client.login.side_effect = [Exception("Invalid session"), None]
        mock_atproto_client.return_value = mock_client

        # Execute
        client = create_client("alice.bsky.social", "password123")

        # Verify - should have tried session, then fallen back to handle/password
        assert client == mock_client
        assert mock_client.login.call_count == 2
        # First call with session string
        mock_client.login.assert_any_call(session_string="invalid-session")
        # Second call with credentials
        mock_client.login.assert_any_call("alice.bsky.social", "password123")

    def test_rejects_session_for_different_user(
        self, mock_atproto_client, tmp_path, monkeypatch
    ):
        """Should not reuse session if handle doesn't match."""
        # Setup - session exists but for different user
        session_file = tmp_path / "session.json"
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: session_file,
        )
        session_file.write_text("bob-session-string")
        session_file.chmod(0o600)

        mock_client = MagicMock()
        # First login (session) returns different user
        mock_client.me = SimpleNamespace(handle="bob.bsky.social", did="did:plc:bob123")
        mock_client.export_session_string.return_value = "alice-new-session"
        mock_atproto_client.return_value = mock_client

        # Execute - trying to login as alice
        client = create_client("alice.bsky.social", "password123")

        # Verify - should have rejected session and done full login
        assert client == mock_client
        assert mock_client.login.call_count == 2
        # Second call should be with credentials
        mock_client.login.assert_any_call("alice.bsky.social", "password123")

    def test_raises_on_authentication_failure(
        self, mock_atproto_client, tmp_path, monkeypatch
    ):
        """Should raise exception when authentication fails."""
        # Setup - no existing session
        monkeypatch.setattr(
            "lestash_bluesky.client.get_session_path",
            lambda: tmp_path / "session.json",
        )

        mock_client = MagicMock()
        mock_client.login.side_effect = Exception("Invalid credentials")
        mock_atproto_client.return_value = mock_client

        # Execute and verify
        with pytest.raises(Exception, match="Authentication failed"):
            create_client("alice.bsky.social", "wrong-password")

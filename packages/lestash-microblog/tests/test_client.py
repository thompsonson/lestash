"""Tests for Micropub client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from lestash_microblog.client import (
    MICROBLOG_API_BASE,
    MICROPUB_ENDPOINT,
    MicropubClient,
    create_client,
)


class TestMicropubClientInit:
    """Test MicropubClient initialization."""

    def test_init_with_provided_token(self, tmp_path, monkeypatch):
        """Should use provided token."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        client = MicropubClient(token="provided-token")

        assert client.token == "provided-token"
        client.close()

    def test_init_loads_token_from_config(self, tmp_path, monkeypatch):
        """Should load token from config when not provided."""
        from lestash_microblog.client import save_token

        token_file = tmp_path / "token.json"
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: token_file,
        )

        save_token("saved-token")
        client = MicropubClient()

        assert client.token == "saved-token"
        client.close()

    def test_init_raises_when_no_token(self, tmp_path, monkeypatch):
        """Should raise ValueError when no token available."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "nonexistent.json",
        )

        with pytest.raises(ValueError, match="No token provided"):
            MicropubClient()

    def test_init_uses_default_endpoint(self, tmp_path, monkeypatch):
        """Should use default Micropub endpoint."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        client = MicropubClient(token="test")

        assert client.endpoint == MICROPUB_ENDPOINT
        client.close()

    def test_init_accepts_custom_endpoint(self, tmp_path, monkeypatch):
        """Should accept custom endpoint."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        client = MicropubClient(token="test", endpoint="https://custom.endpoint/micropub")

        assert client.endpoint == "https://custom.endpoint/micropub"
        client.close()


class TestMicropubClientContextManager:
    """Test MicropubClient context manager."""

    def test_context_manager_enter(self, tmp_path, monkeypatch):
        """Should return self on enter."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        with MicropubClient(token="test") as client:
            assert isinstance(client, MicropubClient)

    def test_context_manager_closes_client(self, tmp_path, monkeypatch):
        """Should close httpx client on exit."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        with MicropubClient(token="test") as client:
            mock_client = MagicMock()
            client._client = mock_client

        mock_client.close.assert_called_once()


class TestMicropubClientGetConfig:
    """Test MicropubClient.get_config()."""

    def test_get_config_success(self, tmp_path, monkeypatch, mock_micropub_config):
        """Should return config dict."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = mock_micropub_config

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            config = client.get_config()
            client.close()

        assert "destination" in config
        assert "media-endpoint" in config

    def test_get_config_raises_on_error(self, tmp_path, monkeypatch):
        """Should raise on HTTP error."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=MagicMock(),
        )

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="invalid")
            with pytest.raises(httpx.HTTPStatusError):
                client.get_config()
            client.close()


class TestMicropubClientGetDestinations:
    """Test MicropubClient.get_destinations()."""

    def test_get_destinations_returns_list(self, tmp_path, monkeypatch, mock_micropub_config):
        """Should return list of destinations."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = mock_micropub_config

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            destinations = client.get_destinations()
            client.close()

        assert len(destinations) == 2
        assert destinations[0]["name"] == "Main Blog"
        assert destinations[1]["name"] == "Photos"

    def test_get_destinations_returns_empty_when_none(self, tmp_path, monkeypatch):
        """Should return empty list when no destinations."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"media-endpoint": "https://example.com"}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            destinations = client.get_destinations()
            client.close()

        assert destinations == []


class TestMicropubClientGetPosts:
    """Test MicropubClient.get_posts()."""

    def test_get_posts_returns_items(self, tmp_path, monkeypatch, mock_micropub_posts):
        """Should return list of posts."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": mock_micropub_posts}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            posts = client.get_posts()
            client.close()

        assert len(posts) == 3

    def test_get_posts_with_pagination(self, tmp_path, monkeypatch, microblog_post_factory):
        """Should pass limit and offset parameters."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response) as mock_get:
            client = MicropubClient(token="test")
            client.get_posts(limit=50, offset=100)
            client.close()

        call_args = mock_get.call_args
        params = call_args[1]["params"]
        assert params["limit"] == 50
        assert params["offset"] == 100

    def test_get_posts_with_destination(self, tmp_path, monkeypatch):
        """Should include destination parameter."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response) as mock_get:
            client = MicropubClient(token="test")
            client.get_posts(destination="https://blog.example.com/")
            client.close()

        call_args = mock_get.call_args
        params = call_args[1]["params"]
        assert params["mp-destination"] == "https://blog.example.com/"

    def test_get_posts_returns_empty_when_no_items(self, tmp_path, monkeypatch):
        """Should return empty list when no items key."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            posts = client.get_posts()
            client.close()

        assert posts == []


class TestMicropubClientGetAllPosts:
    """Test MicropubClient.get_all_posts()."""

    def test_get_all_posts_single_page(self, tmp_path, monkeypatch, mock_micropub_posts):
        """Should return all posts when single page."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": mock_micropub_posts}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            posts = client.get_all_posts(limit=100)
            client.close()

        assert len(posts) == 3

    def test_get_all_posts_multiple_pages(self, tmp_path, monkeypatch, microblog_post_factory):
        """Should paginate through all posts."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        page1 = [microblog_post_factory(content=f"Post {i}") for i in range(5)]
        page2 = [microblog_post_factory(content=f"Post {i}") for i in range(5, 8)]

        mock_responses = [
            MagicMock(json=MagicMock(return_value={"items": page1})),
            MagicMock(json=MagicMock(return_value={"items": page2})),
        ]

        with patch.object(httpx.Client, "get", side_effect=mock_responses):
            client = MicropubClient(token="test")
            posts = client.get_all_posts(limit=5)
            client.close()

        assert len(posts) == 8

    def test_get_all_posts_respects_max_posts(self, tmp_path, monkeypatch, microblog_post_factory):
        """Should stop at max_posts limit."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        posts = [microblog_post_factory(content=f"Post {i}") for i in range(10)]
        mock_response = MagicMock()
        mock_response.json.return_value = {"items": posts}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            result = client.get_all_posts(limit=100, max_posts=5)
            client.close()

        assert len(result) == 5

    def test_get_all_posts_stops_on_empty_response(self, tmp_path, monkeypatch):
        """Should stop when empty response received."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            posts = client.get_all_posts()
            client.close()

        assert posts == []


class TestMicropubClientGetMentions:
    """Test MicropubClient.get_mentions()."""

    def test_get_mentions_returns_items(self, tmp_path, monkeypatch):
        """Should return list of JSON Feed mention items."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mention_items = [
            {
                "id": "111",
                "url": "https://other.micro.blog/2024/01/reply.html",
                "content_text": "Nice post!",
                "date_published": "2024-01-15T12:00:00+00:00",
                "author": {"name": "other_user", "url": "https://micro.blog/other_user"},
                "_microblog": {"id": 111, "is_mention": True},
            }
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": mention_items}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            mentions = client.get_mentions()
            client.close()

        assert len(mentions) == 1
        assert mentions[0]["content_text"] == "Nice post!"

    def test_get_mentions_passes_pagination_params(self, tmp_path, monkeypatch):
        """Should pass count and before_id parameters."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response) as mock_get:
            client = MicropubClient(token="test")
            client.get_mentions(count=50, before_id=999)
            client.close()

        call_args = mock_get.call_args
        assert call_args[1]["params"]["count"] == 50
        assert call_args[1]["params"]["before_id"] == 999

    def test_get_mentions_uses_api_base_url(self, tmp_path, monkeypatch):
        """Should use MICROBLOG_API_BASE, not Micropub endpoint."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response) as mock_get:
            client = MicropubClient(token="test")
            client.get_mentions()
            client.close()

        url = mock_get.call_args[0][0]
        assert url.startswith(MICROBLOG_API_BASE)
        assert "/posts/mentions" in url


class TestMicropubClientGetAllMentions:
    """Test MicropubClient.get_all_mentions()."""

    def test_get_all_mentions_paginates(self, tmp_path, monkeypatch):
        """Should paginate through mentions using before_id."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        page1 = [
            {"id": str(i), "_microblog": {"id": i}, "content_text": f"M{i}"}
            for i in range(5, 0, -1)
        ]
        page2 = [{"id": str(i), "_microblog": {"id": i}, "content_text": f"M{i}"} for i in range(3)]

        mock_responses = [
            MagicMock(json=MagicMock(return_value={"items": page1})),
            MagicMock(json=MagicMock(return_value={"items": page2})),
        ]

        with patch.object(httpx.Client, "get", side_effect=mock_responses):
            client = MicropubClient(token="test")
            mentions = client.get_all_mentions(count=5)
            client.close()

        assert len(mentions) == 8

    def test_get_all_mentions_respects_max_items(self, tmp_path, monkeypatch):
        """Should stop at max_items."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        items = [
            {"id": str(i), "_microblog": {"id": i}, "content_text": f"M{i}"} for i in range(10)
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"items": items}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            result = client.get_all_mentions(count=100, max_items=3)
            client.close()

        assert len(result) == 3


class TestMicropubClientGetConversation:
    """Test MicropubClient.get_conversation()."""

    def test_get_conversation_passes_id(self, tmp_path, monkeypatch):
        """Should pass post ID as query parameter."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": []}

        with patch.object(httpx.Client, "get", return_value=mock_response) as mock_get:
            client = MicropubClient(token="test")
            client.get_conversation("12345")
            client.close()

        call_args = mock_get.call_args
        assert call_args[1]["params"]["id"] == "12345"

    def test_get_conversation_returns_thread(self, tmp_path, monkeypatch):
        """Should return all items in the thread."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        thread = [
            {"id": "100", "content_text": "Original post"},
            {"id": "101", "content_text": "Reply to original"},
            {"id": "102", "content_text": "Reply to reply"},
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = {"items": thread}

        with patch.object(httpx.Client, "get", return_value=mock_response):
            client = MicropubClient(token="test")
            result = client.get_conversation("100")
            client.close()

        assert len(result) == 3


class TestCreateClient:
    """Test create_client helper function."""

    def test_create_client_returns_micropub_client(self, tmp_path, monkeypatch):
        """Should return MicropubClient instance."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "token.json",
        )

        client = create_client(token="test-token")

        assert isinstance(client, MicropubClient)
        client.close()

    def test_create_client_with_no_token_raises(self, tmp_path, monkeypatch):
        """Should raise when no token available."""
        monkeypatch.setattr(
            "lestash_microblog.client.get_token_path",
            lambda: tmp_path / "nonexistent.json",
        )

        with pytest.raises(ValueError, match="No token provided"):
            create_client()

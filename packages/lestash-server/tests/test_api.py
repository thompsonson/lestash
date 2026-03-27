"""Tests for the LeStash API server endpoints."""

import io
import json
from unittest.mock import MagicMock, patch


class TestHealth:
    """Test /api/health endpoint."""

    def test_health_returns_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"]
        assert data["items"] == 5


class TestItems:
    """Test /api/items endpoints."""

    def test_list_items(self, client):
        resp = client.get("/api/items")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 5
        assert data["limit"] == 20
        assert data["offset"] == 0

    def test_list_items_with_limit(self, client):
        resp = client.get("/api/items?limit=2")
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5

    def test_list_items_with_offset(self, client):
        resp = client.get("/api/items?limit=2&offset=3")
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5

    def test_list_items_filter_by_source(self, client):
        resp = client.get("/api/items?source=linkedin")
        data = resp.json()
        assert data["total"] == 2
        assert all(i["source_type"] == "linkedin" for i in data["items"])

    def test_list_items_filter_own(self, client):
        resp = client.get("/api/items?own=true")
        data = resp.json()
        assert data["total"] == 2
        assert all(i["is_own_content"] for i in data["items"])

    def test_list_items_enriched_fields(self, client):
        resp = client.get("/api/items?limit=1&source=linkedin")
        item = resp.json()["items"][0]
        assert "subtype" in item
        assert "author_display" in item
        assert "actor_display" in item
        assert "preview" in item

    def test_get_item_by_id(self, client):
        # First get an item ID
        items = client.get("/api/items?limit=1").json()["items"]
        item_id = items[0]["id"]

        resp = client.get(f"/api/items/{item_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == item_id
        assert "content" in data
        assert "subtype" in data

    def test_get_item_not_found(self, client):
        resp = client.get("/api/items/999999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_search_items(self, client):
        resp = client.get("/api/items/search?q=Python")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) > 0

    def test_search_no_results(self, client):
        resp = client.get("/api/items/search?q=xyznonexistent")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 0

    def test_search_requires_query(self, client):
        resp = client.get("/api/items/search")
        assert resp.status_code == 422  # validation error

    def test_list_items_exclude_subtype(self, client):
        """Excluding 'reaction' should filter out linkedin likes."""
        resp = client.get("/api/items?exclude_subtype=reaction")
        data = resp.json()
        subtypes = [i["subtype"] for i in data["items"]]
        assert all("reaction" not in s for s in subtypes)

    def test_list_items_exclude_multiple_subtypes(self, client):
        """Excluding multiple subtypes should filter all of them."""
        resp = client.get("/api/items?exclude_subtype=reaction,post")
        data = resp.json()
        subtypes = [i["subtype"] for i in data["items"]]
        assert all("reaction" not in s and "post" not in s for s in subtypes)

    def test_list_items_exclude_subtype_default_returns_all(self, client):
        """No exclude param should return all items."""
        resp = client.get("/api/items")
        assert resp.json()["total"] == 5

    def test_list_items_since_filter(self, client):
        """Since filter should only return recently fetched items."""
        # All test items were just inserted, so a recent 'since' should find them
        resp = client.get("/api/items?since=2020-01-01T00:00:00")
        data = resp.json()
        assert data["total"] == 5

    def test_list_items_since_far_future(self, client):
        """Since in the future should return no items."""
        resp = client.get("/api/items?since=2099-01-01T00:00:00")
        data = resp.json()
        assert data["total"] == 0
        assert len(data["items"]) == 0


class TestSources:
    """Test /api/sources endpoints."""

    def test_list_sources(self, client):
        resp = client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        source = data[0]
        assert "name" in source
        assert "description" in source
        assert "enabled" in source

    def test_source_status(self, client):
        resp = client.get("/api/sources/status")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_sync_unknown_source(self, client):
        resp = client.post("/api/sources/nonexistent/sync")
        assert resp.status_code == 404

    def test_sync_known_source(self, client):
        # arxiv sync won't actually fetch anything without a query,
        # but the endpoint should accept the request
        resp = client.post("/api/sources/arxiv/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["source"] == "arxiv"


class TestProfiles:
    """Test /api/profiles endpoint."""

    def test_list_profiles_empty(self, client):
        resp = client.get("/api/profiles")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestStats:
    """Test /api/stats endpoint."""

    def test_stats(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_items"] == 5
        assert data["own_content"] == 2
        assert "linkedin" in data["sources"]
        assert data["sources"]["linkedin"] == 2
        assert data["sources"]["bluesky"] == 1
        assert data["sources"]["youtube"] == 1
        assert data["sources"]["arxiv"] == 1
        assert data["date_range"]["earliest"] is not None
        assert data["date_range"]["latest"] is not None


class TestCreateItem:
    """Test POST /api/items endpoint."""

    def test_create_item(self, client):
        resp = client.post(
            "/api/items",
            json={"source_type": "test", "content": "Hello from API", "title": "Test Item"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["source_type"] == "test"
        assert data["content"] == "Hello from API"
        assert data["is_own_content"] is True

    def test_create_item_missing_content(self, client):
        resp = client.post("/api/items", json={"source_type": "test"})
        assert resp.status_code == 422

    def test_create_item_upsert(self, client):
        """Creating same item twice should update, not duplicate."""
        body = {"source_type": "test", "source_id": "upsert-1", "content": "v1"}
        client.post("/api/items", json=body)
        body["content"] = "v2"
        resp = client.post("/api/items", json=body)
        assert resp.status_code == 201
        assert resp.json()["content"] == "v2"


class TestImport:
    """Test POST /api/import endpoint."""

    def test_import_json_file(self, client):
        items = [
            {"source_type": "note", "content": "Note 1", "title": "First"},
            {"source_type": "note", "content": "Note 2"},
        ]
        data = json.dumps(items).encode()
        resp = client.post(
            "/api/import",
            files={"file": ("test.json", io.BytesIO(data), "application/json")},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] == "completed"
        assert result["items_added"] == 2
        assert result["source_type"] == "json"

    def test_import_empty_json(self, client):
        resp = client.post(
            "/api/import",
            files={"file": ("empty.json", io.BytesIO(b"[]"), "application/json")},
        )
        assert resp.status_code == 200
        assert resp.json()["items_added"] == 0

    def test_import_invalid_json(self, client):
        resp = client.post(
            "/api/import",
            files={"file": ("bad.json", io.BytesIO(b"not json"), "application/json")},
        )
        assert resp.status_code == 400

    def test_import_missing_content(self, client):
        data = json.dumps([{"source_type": "test"}]).encode()
        resp = client.post(
            "/api/import",
            files={"file": ("bad.json", io.BytesIO(data), "application/json")},
        )
        assert resp.status_code == 400


class TestVoiceRefine:
    """Test POST /api/voice/refine endpoint."""

    def test_refine_success(self, client):
        """Should return refined text from LLM."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Cleaned up text."}}],
            "model": "test-model",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("lestash_server.routes.voice.httpx.post", return_value=mock_resp):
            resp = client.post(
                "/api/voice/refine",
                json={"text": "um so I was thinking about uh the project"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["refined_text"] == "Cleaned up text."
        assert data["model_used"] == "test-model"
        assert "prompt_used" in data

    def test_refine_custom_prompt(self, client):
        """Should use custom prompt when provided."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Summarized."}}],
            "model": "test-model",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("lestash_server.routes.voice.httpx.post", return_value=mock_resp) as mock_post:
            resp = client.post(
                "/api/voice/refine",
                json={"text": "some text", "prompt": "Summarize this."},
            )

        assert resp.status_code == 200
        assert resp.json()["prompt_used"] == "Summarize this."
        call_body = mock_post.call_args[1]["json"]
        assert call_body["messages"][0]["content"] == "Summarize this."

    def test_refine_llm_unreachable(self, client):
        """Should return 503 when LLM proxy is unreachable."""
        with patch(
            "lestash_server.routes.voice.httpx.post",
            side_effect=__import__("httpx").ConnectError("Connection refused"),
        ):
            resp = client.post("/api/voice/refine", json={"text": "test"})

        assert resp.status_code == 503
        assert "not reachable" in resp.json()["detail"]

    def test_refine_missing_text(self, client):
        """Should return 422 when text is missing."""
        resp = client.post("/api/voice/refine", json={})
        assert resp.status_code == 422


class TestVoiceUpload:
    """Test POST /api/voice/upload endpoint."""

    def test_upload_audio(self, client, tmp_path, monkeypatch):
        """Should save audio file and return path."""
        monkeypatch.setattr("lestash_server.routes.voice.Path.home", lambda: tmp_path)

        resp = client.post(
            "/api/voice/upload",
            files={"file": ("test.wav", io.BytesIO(b"fake audio data"), "audio/wav")},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["path"].startswith("voice/")
        assert data["size"] == len(b"fake audio data")


class TestVoiceTranscribe:
    """Test POST /api/voice/transcribe endpoint."""

    def test_transcribe_success(self, client, monkeypatch, tmp_path):
        """Should transcribe audio file and save as item."""
        monkeypatch.setattr("lestash_server.routes.voice.Path.home", lambda: tmp_path)

        mock_result = MagicMock()
        mock_result.text = "Hello, this is a test transcription."
        mock_result.language = "en"
        mock_result.duration_seconds = 3.5
        mock_result.model = "base.en"

        with patch("lestash_voice.transcribe.transcribe_file", return_value=mock_result):
            resp = client.post(
                "/api/voice/transcribe",
                files={"file": ("recording.mp3", io.BytesIO(b"fake mp3 data"), "audio/mpeg")},
                data={"model": "base.en"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["text"] == "Hello, this is a test transcription."
        assert data["language"] == "en"
        assert data["duration_seconds"] == 3.5
        assert data["model"] == "base.en"
        assert data["item_id"] > 0
        assert data["title"] == "recording"

    def test_transcribe_custom_title(self, client, monkeypatch, tmp_path):
        """Should use custom title when provided."""
        monkeypatch.setattr("lestash_server.routes.voice.Path.home", lambda: tmp_path)

        mock_result = MagicMock()
        mock_result.text = "Meeting notes."
        mock_result.language = "en"
        mock_result.duration_seconds = 60.0
        mock_result.model = "base.en"

        with patch("lestash_voice.transcribe.transcribe_file", return_value=mock_result):
            resp = client.post(
                "/api/voice/transcribe",
                files={"file": ("meeting.m4a", io.BytesIO(b"fake m4a"), "audio/mp4")},
                data={"title": "Team standup 2026-03-26"},
            )

        assert resp.status_code == 201
        assert resp.json()["title"] == "Team standup 2026-03-26"

    def test_transcribe_unsupported_format(self, client):
        """Should reject unsupported file formats."""
        resp = client.post(
            "/api/voice/transcribe",
            files={"file": ("video.avi", io.BytesIO(b"fake video"), "video/avi")},
        )
        assert resp.status_code == 400
        assert "Unsupported format" in resp.json()["detail"]

    def test_transcribe_no_speech(self, client, monkeypatch, tmp_path):
        """Should return 422 when no speech is detected."""
        monkeypatch.setattr("lestash_server.routes.voice.Path.home", lambda: tmp_path)

        mock_result = MagicMock()
        mock_result.text = ""
        mock_result.language = "en"
        mock_result.duration_seconds = 2.0
        mock_result.model = "base.en"

        with patch("lestash_voice.transcribe.transcribe_file", return_value=mock_result):
            resp = client.post(
                "/api/voice/transcribe",
                files={"file": ("silence.wav", io.BytesIO(b"silence"), "audio/wav")},
            )

        assert resp.status_code == 422
        assert "No speech" in resp.json()["detail"]

    def test_transcribe_file_too_large(self, client):
        """Should reject files over 50MB."""
        big_data = b"x" * (51 * 1024 * 1024)
        resp = client.post(
            "/api/voice/transcribe",
            files={"file": ("huge.mp3", io.BytesIO(big_data), "audio/mpeg")},
        )
        assert resp.status_code == 413


class TestCORS:
    """Test CORS headers."""

    def test_cors_tauri_origin(self, client):
        resp = client.get("/api/health", headers={"Origin": "tauri://localhost"})
        assert resp.headers.get("access-control-allow-origin") == "tauri://localhost"

    def test_cors_tailscale_origin(self, client):
        resp = client.get(
            "/api/health",
            headers={"Origin": "https://pop-mini.monkey-ladon.ts.net:8444"},
        )
        assert "ts.net" in resp.headers.get("access-control-allow-origin", "")

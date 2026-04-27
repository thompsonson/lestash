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


class TestItemPatch:
    """Test PATCH /api/items/{id} — user edits create history rows tagged 'user-edit'."""

    def _item_id(self, client, source: str = "linkedin", own: bool | None = None) -> int:
        url = f"/api/items?source={source}&limit=1"
        if own is not None:
            url += f"&own={'true' if own else 'false'}"
        items = client.get(url).json()["items"]
        return items[0]["id"]

    def _history(self, test_config, item_id: int):
        from lestash.core.database import get_connection

        with get_connection(test_config) as conn:
            rows = conn.execute(
                "SELECT change_reason, title_old, content_old, parent_id_old "
                "FROM item_history WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def test_patch_title_creates_user_edit_history(self, client, test_config):
        item_id = self._item_id(client, own=True)
        resp = client.patch(f"/api/items/{item_id}", json={"title": "Edited Title"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Edited Title"

        history = self._history(test_config, item_id)
        assert len(history) == 1
        assert history[0]["change_reason"] == "user-edit"
        assert history[0]["title_old"] == "First Post"

    def test_patch_content_creates_user_edit_history(self, client, test_config):
        item_id = self._item_id(client, own=True)
        resp = client.patch(f"/api/items/{item_id}", json={"content": "New body"})
        assert resp.status_code == 200

        history = self._history(test_config, item_id)
        assert len(history) == 1
        assert history[0]["change_reason"] == "user-edit"
        assert history[0]["content_old"] == "My first LinkedIn post about Python"

    def test_patch_parent_id_creates_user_edit_history(self, client, test_config):
        # Use bluesky item; reparent under linkedin item
        bluesky_id = self._item_id(client, "bluesky")
        linkedin_id = self._item_id(client, "linkedin", own=True)

        resp = client.patch(f"/api/items/{bluesky_id}", json={"parent_id": linkedin_id})
        assert resp.status_code == 200

        history = self._history(test_config, bluesky_id)
        assert len(history) == 1
        assert history[0]["change_reason"] == "user-edit"
        assert history[0]["parent_id_old"] is None

    def test_patch_no_change_creates_no_history(self, client, test_config):
        item_id = self._item_id(client, own=True)
        current = client.get(f"/api/items/{item_id}").json()

        resp = client.patch(
            f"/api/items/{item_id}",
            json={"title": current["title"], "content": current["content"]},
        )
        assert resp.status_code == 200

        history = self._history(test_config, item_id)
        assert history == []


class TestItemHistory:
    """Test the GET/POST /api/items/{id}/history* endpoints."""

    def _seeded_item(self, client) -> dict:
        items = client.get("/api/items?source=linkedin&own=true&limit=1").json()["items"]
        return items[0]

    def test_history_list_empty_for_unedited_item(self, client):
        item = self._seeded_item(client)
        resp = client.get(f"/api/items/{item['id']}/history")
        assert resp.status_code == 200
        assert resp.json() == {"versions": []}

    def test_history_list_returns_versions_newest_first(self, client):
        item = self._seeded_item(client)
        client.patch(f"/api/items/{item['id']}", json={"title": "Edit 1"})
        client.patch(f"/api/items/{item['id']}", json={"title": "Edit 2"})

        resp = client.get(f"/api/items/{item['id']}/history")
        versions = resp.json()["versions"]
        assert len(versions) == 2
        assert versions[0]["title_old"] == "Edit 1"
        assert versions[1]["title_old"] == item["title"]
        assert all(v["change_reason"] == "user-edit" for v in versions)

    def test_history_list_404_for_missing_item(self, client):
        resp = client.get("/api/items/999999/history")
        assert resp.status_code == 404

    def test_history_detail_returns_full_snapshot(self, client):
        item = self._seeded_item(client)
        client.patch(f"/api/items/{item['id']}", json={"content": "Edited content"})

        version_id = client.get(f"/api/items/{item['id']}/history").json()["versions"][0]["id"]

        resp = client.get(f"/api/items/{item['id']}/history/{version_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["item_id"] == item["id"]
        assert body["content_old"] == item["content"]
        assert body["title_old"] == item["title"]
        assert body["change_reason"] == "user-edit"
        assert body["metadata_old"] == item["metadata"]

    def test_history_detail_404_for_missing_version(self, client):
        item = self._seeded_item(client)
        resp = client.get(f"/api/items/{item['id']}/history/999999")
        assert resp.status_code == 404

    def test_history_detail_404_for_mismatched_item(self, client):
        # Edit item A, then ask for that version under item B's ID.
        a = self._seeded_item(client)
        b_items = client.get("/api/items?source=bluesky&limit=1").json()["items"]
        b_id = b_items[0]["id"]
        client.patch(f"/api/items/{a['id']}", json={"title": "Edit"})
        version_id = client.get(f"/api/items/{a['id']}/history").json()["versions"][0]["id"]

        resp = client.get(f"/api/items/{b_id}/history/{version_id}")
        assert resp.status_code == 404

    def test_restore_reverts_to_snapshot(self, client):
        item = self._seeded_item(client)
        original_title = item["title"]
        original_content = item["content"]

        client.patch(
            f"/api/items/{item['id']}",
            json={"title": "Bad rename", "content": "Bad content"},
        )
        version_id = client.get(f"/api/items/{item['id']}/history").json()["versions"][0]["id"]

        resp = client.post(f"/api/items/{item['id']}/history/{version_id}/restore")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == original_title
        assert body["content"] == original_content

    def test_restore_creates_new_history_row_tagged_restore(self, client):
        item = self._seeded_item(client)
        client.patch(f"/api/items/{item['id']}", json={"title": "Bad rename"})
        version_id = client.get(f"/api/items/{item['id']}/history").json()["versions"][0]["id"]

        client.post(f"/api/items/{item['id']}/history/{version_id}/restore")

        versions = client.get(f"/api/items/{item['id']}/history").json()["versions"]
        assert len(versions) == 2
        # The newest version captures the pre-restore state ("Bad rename").
        assert versions[0]["change_reason"] == "restore"
        assert versions[0]["title_old"] == "Bad rename"

    def test_restore_404_for_unknown_version(self, client):
        item = self._seeded_item(client)
        resp = client.post(f"/api/items/{item['id']}/history/999999/restore")
        assert resp.status_code == 404


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

    def test_import_pdf_extracts_and_attaches_media(self, client, test_config):
        """PDF import should extract markdown via Docling and save the
        original as a media attachment of type 'source_pdf' (so the enricher
        can find it for re-runs)."""
        from lestash.core.database import get_connection, get_media_dir

        pdf_bytes = b"%PDF-1.4 fake pdf contents for test"
        with patch(
            "lestash.core.text_extract.convert_to_markdown",
            return_value="# Extracted\n\nhello",
        ) as mock_conv:
            resp = client.post(
                "/api/import",
                files={
                    "file": ("report.pdf", io.BytesIO(pdf_bytes), "application/pdf"),
                },
            )

        assert resp.status_code == 200
        result = resp.json()
        assert result["status"] == "completed"
        assert result["source_type"] == "pdf"
        assert result["items_added"] == 1
        mock_conv.assert_called_once()

        with get_connection(test_config) as conn:
            row = conn.execute(
                "SELECT id, title, content, source_type FROM items WHERE source_type = 'pdf'"
            ).fetchone()
            assert row is not None
            assert row["title"] == "report"
            assert "# Extracted" in row["content"]

            media = conn.execute(
                "SELECT media_type, mime_type, local_path, source_origin "
                "FROM item_media WHERE item_id = ?",
                (row["id"],),
            ).fetchone()
            assert media is not None
            assert media["media_type"] == "source_pdf"
            assert media["mime_type"] == "application/pdf"
            assert media["source_origin"] == "upload"

            saved = get_media_dir(test_config) / media["local_path"]
            assert saved.is_file()
            assert saved.read_bytes() == pdf_bytes

    def test_import_pdf_docling_failure_keeps_original(self, client, test_config):
        """If Docling returns nothing, the item is still created with a
        placeholder body and the PDF is still saved as media."""
        from lestash.core.database import get_connection

        pdf_bytes = b"%PDF-1.4 unreadable"
        with patch(
            "lestash.core.text_extract.convert_to_markdown",
            return_value="",
        ):
            resp = client.post(
                "/api/import",
                files={
                    "file": ("scan.pdf", io.BytesIO(pdf_bytes), "application/pdf"),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["items_added"] == 1

        with get_connection(test_config) as conn:
            row = conn.execute("SELECT content FROM items WHERE source_type = 'pdf'").fetchone()
            assert row is not None
            assert row["content"] == "[PDF: scan.pdf]"

    def test_import_pdf_idempotent_by_content_hash(self, client, test_config):
        """Re-importing the same PDF bytes must not create a duplicate item."""
        from lestash.core.database import get_connection

        pdf_bytes = b"%PDF-1.4 same-content"
        with patch(
            "lestash.core.text_extract.convert_to_markdown",
            return_value="# md",
        ):
            r1 = client.post(
                "/api/import",
                files={"file": ("a.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            )
            r2 = client.post(
                "/api/import",
                files={"file": ("b.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            )

        assert r1.status_code == 200
        assert r2.status_code == 200

        with get_connection(test_config) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE source_type = 'pdf'"
            ).fetchone()
            assert rows["n"] == 1


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


class TestTags:
    """Test tag endpoints."""

    def _create_item(self, client):
        resp = client.post(
            "/api/items",
            json={
                "source_type": "test",
                "source_id": "tag-test-1",
                "content": "Tag test item",
            },
        )
        return resp.json()["id"]

    def test_add_tag(self, client):
        """Should add a tag to an item."""
        item_id = self._create_item(client)
        resp = client.post(f"/api/items/{item_id}/tags", json={"name": "important"})
        assert resp.status_code == 201
        assert "important" in resp.json()["tags"]

    def test_add_duplicate_tag(self, client):
        """Adding the same tag twice should be idempotent."""
        item_id = self._create_item(client)
        client.post(f"/api/items/{item_id}/tags", json={"name": "dup"})
        resp = client.post(f"/api/items/{item_id}/tags", json={"name": "dup"})
        assert resp.status_code == 201
        assert resp.json()["tags"].count("dup") == 1

    def test_remove_tag(self, client):
        """Should remove a tag from an item."""
        item_id = self._create_item(client)
        client.post(f"/api/items/{item_id}/tags", json={"name": "remove-me"})
        resp = client.delete(f"/api/items/{item_id}/tags/remove-me")
        assert resp.status_code == 200
        assert "remove-me" not in resp.json()["tags"]

    def test_list_tags(self, client):
        """Should list all tags with counts."""
        item_id = self._create_item(client)
        client.post(f"/api/items/{item_id}/tags", json={"name": "alpha"})
        client.post(f"/api/items/{item_id}/tags", json={"name": "beta"})
        resp = client.get("/api/items/tags")
        assert resp.status_code == 200
        tags = {t["name"]: t["count"] for t in resp.json()["tags"]}
        assert "alpha" in tags
        assert "beta" in tags

    def test_filter_by_tag(self, client):
        """Should filter items by tag."""
        id1 = self._create_item(client)
        self._create_item(client)  # untagged
        client.post(f"/api/items/{id1}/tags", json={"name": "filtered"})
        resp = client.get("/api/items?tag=filtered")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        assert all("filtered" in i["tags"] for i in items)

    def test_tag_on_nonexistent_item(self, client):
        """Should return 404 for missing item."""
        resp = client.post("/api/items/99999/tags", json={"name": "nope"})
        assert resp.status_code == 404


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

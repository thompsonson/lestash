"""Tests for the LeStash API server endpoints."""


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

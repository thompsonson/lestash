"""Tests for the HTTP sync server.

Tests use FastAPI's TestClient for synchronous testing without starting a real server.
Uses the production schema via init_database(), which is CRR-compatible after migration 3.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from lestash.cli.sync import SYNC_TABLES
from lestash.core import crsqlite
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_crdt_connection, init_database

try:
    from fastapi.testclient import TestClient
    from lestash.server.app import _serialize_change, create_app

    SERVER_AVAILABLE = True
except ImportError:
    SERVER_AVAILABLE = False


def _crsqlite_available() -> bool:
    """Check if cr-sqlite can be loaded."""
    if not crsqlite.is_extension_available():
        return False
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            conn = sqlite3.connect(db_path)
            result = crsqlite.load_extension(conn)
            if result:
                crsqlite.finalize_connection(conn)
            conn.close()
            return result
    except Exception:
        return False


crsqlite_and_server = pytest.mark.skipif(
    not (SERVER_AVAILABLE and _crsqlite_available()),
    reason="Requires both cr-sqlite extension and FastAPI",
)


def _serialize_changes(changes: list[dict]) -> list[dict]:
    """Serialize a list of changes for JSON transport."""
    return [_serialize_change(c) for c in changes]


def _create_peer_db(tmpdir: str, name: str = "peer.db") -> Config:
    """Create a peer database with CRR-enabled items table."""
    db_path = Path(tmpdir) / name
    config = Config(general=GeneralConfig(database_path=str(db_path)))
    init_database(config)
    with get_crdt_connection(config) as conn:
        for table in SYNC_TABLES:
            crsqlite.upgrade_to_crr(conn, table)
    return config


@pytest.fixture
def sync_server():
    """Create a test server with a temporary CRDT-enabled database.

    Uses the production schema via init_database() — after migration 3,
    all tables are CRR-compatible (no UNIQUE constraints, DEFAULTs on NOT NULL).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_sync.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)

        with get_crdt_connection(config) as conn:
            for table in SYNC_TABLES:
                assert crsqlite.upgrade_to_crr(conn, table), f"Failed to upgrade {table} to CRR"

        app = create_app(config)
        client = TestClient(app)
        yield client, config


@crsqlite_and_server
class TestSyncStatusEndpoint:
    """Tests for GET /sync/status."""

    def test_returns_200(self, sync_server):
        client, _ = sync_server
        response = client.get("/sync/status")
        assert response.status_code == 200

    def test_site_id_is_valid_hex(self, sync_server):
        client, _ = sync_server
        data = client.get("/sync/status").json()
        site_id = data["site_id"]
        assert len(site_id) > 0
        # Verify it's valid hex by round-tripping
        bytes.fromhex(site_id)

    def test_db_version_is_non_negative(self, sync_server):
        client, _ = sync_server
        data = client.get("/sync/status").json()
        assert data["db_version"] >= 0

    def test_protocol_fields_match_spec(self, sync_server):
        client, _ = sync_server
        data = client.get("/sync/status").json()
        protocol = data["protocol"]
        assert protocol["version"] == 1
        assert protocol["format"] == "lestash-crsqlite-v1"
        assert protocol["schema_version"] == 3
        assert protocol["crsqlite_version"] == "0.16.3"
        assert set(SYNC_TABLES) == set(protocol["sync_tables"])


@crsqlite_and_server
class TestGetChangesEndpoint:
    """Tests for GET /sync/changes."""

    def test_returns_empty_changes_for_new_db(self, sync_server):
        client, _ = sync_server
        response = client.get("/sync/changes", params={"since": 0})
        assert response.status_code == 200
        assert response.json()["changes"] == []

    def test_returns_changes_after_insert(self, sync_server):
        client, config = sync_server
        with get_crdt_connection(config) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "item-1", "test content"),
            )
            conn.commit()

        response = client.get("/sync/changes", params={"since": 0})
        assert response.status_code == 200
        changes = response.json()["changes"]
        assert len(changes) > 0
        # Verify the changes reference the items table
        tables = {c["table"] for c in changes}
        assert "items" in tables

    def test_since_filters_old_changes(self, sync_server):
        client, config = sync_server
        with get_crdt_connection(config) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "item-1", "content"),
            )
            conn.commit()
            version_after = crsqlite.get_db_version(conn)

        response = client.get("/sync/changes", params={"since": version_after})
        assert response.json()["changes"] == []

    def test_since_boundary_includes_exact_version(self, sync_server):
        """Changes at exactly the 'since' version should NOT be returned (since is exclusive)."""
        client, config = sync_server

        with get_crdt_connection(config) as conn:
            conn.execute(
                "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                ("test", "item-1", "content"),
            )
            conn.commit()
            version_at = crsqlite.get_db_version(conn)

        # since=version_at should return nothing (changes AT that version are excluded)
        response = client.get("/sync/changes", params={"since": version_at})
        assert response.json()["changes"] == []

        # since=version_at-1 should return the changes
        response = client.get("/sync/changes", params={"since": version_at - 1})
        assert len(response.json()["changes"]) > 0

    def test_default_since_is_zero(self, sync_server):
        client, _ = sync_server
        response = client.get("/sync/changes")
        assert response.status_code == 200
        assert "changes" in response.json()


@crsqlite_and_server
class TestPostChangesEndpoint:
    """Tests for POST /sync/changes."""

    def test_apply_empty_changes(self, sync_server):
        client, _ = sync_server
        response = client.post("/sync/changes", json={"changes": []})
        assert response.status_code == 200
        data = response.json()
        assert data["applied"] == 0
        assert data["db_version"] >= 0

    def test_apply_changes_from_another_db(self, sync_server):
        client, config = sync_server

        with tempfile.TemporaryDirectory() as tmpdir:
            config_b = _create_peer_db(tmpdir)

            with get_crdt_connection(config_b) as conn_b:
                conn_b.execute(
                    "INSERT INTO items (source_type, source_id, content, title) "
                    "VALUES (?, ?, ?, ?)",
                    ("test", "remote-item", "remote content", "Remote Title"),
                )
                conn_b.commit()
                changes = crsqlite.get_changes_since(conn_b, since_version=0)
                change_count = len(changes)

        response = client.post("/sync/changes", json={"changes": _serialize_changes(changes)})
        assert response.status_code == 200
        data = response.json()
        assert data["applied"] == change_count
        assert data["db_version"] > 0

        # Verify item exists in server's database
        with get_crdt_connection(config) as conn:
            cursor = conn.execute(
                "SELECT content, title FROM items WHERE source_id = ?", ("remote-item",)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "remote content"
            assert row[1] == "Remote Title"

    def test_fts_rebuilt_after_apply(self, sync_server):
        client, config = sync_server

        with tempfile.TemporaryDirectory() as tmpdir:
            config_b = _create_peer_db(tmpdir)

            with get_crdt_connection(config_b) as conn_b:
                conn_b.execute(
                    "INSERT INTO items (source_type, source_id, content, title) "
                    "VALUES (?, ?, ?, ?)",
                    ("test", "searchable", "quantum entanglement paper", "Physics"),
                )
                conn_b.commit()
                changes = crsqlite.get_changes_since(conn_b, since_version=0)

        client.post("/sync/changes", json={"changes": _serialize_changes(changes)})

        # Verify FTS search finds the synced item
        with get_crdt_connection(config) as conn:
            cursor = conn.execute(
                "SELECT items.source_id FROM items "
                "JOIN items_fts ON items.id = items_fts.rowid "
                "WHERE items_fts MATCH ?",
                ("quantum",),
            )
            results = cursor.fetchall()
            assert len(results) == 1
            assert results[0][0] == "searchable"

    def test_invalid_request_body(self, sync_server):
        client, _ = sync_server
        response = client.post("/sync/changes", json={"bad": "data"})
        assert response.status_code == 422

    def test_db_version_increments_after_apply(self, sync_server):
        client, _ = sync_server

        version_before = client.get("/sync/status").json()["db_version"]

        with tempfile.TemporaryDirectory() as tmpdir:
            config_b = _create_peer_db(tmpdir)
            with get_crdt_connection(config_b) as conn_b:
                conn_b.execute(
                    "INSERT INTO items (source_type, source_id, content) VALUES (?, ?, ?)",
                    ("test", "v-item", "version test"),
                )
                conn_b.commit()
                changes = crsqlite.get_changes_since(conn_b, since_version=0)

        result = client.post("/sync/changes", json={"changes": _serialize_changes(changes)}).json()
        assert result["db_version"] > version_before


@crsqlite_and_server
class TestRoundtrip:
    """Test full sync roundtrip: insert on peer -> push to server -> pull from server."""

    def test_full_roundtrip(self, sync_server):
        client, config = sync_server

        # 1. Check initial status
        status = client.get("/sync/status").json()
        assert status["db_version"] >= 0
        initial_version = status["db_version"]

        # 2. Create items on a peer database
        with tempfile.TemporaryDirectory() as tmpdir:
            config_peer = _create_peer_db(tmpdir)

            with get_crdt_connection(config_peer) as conn_peer:
                conn_peer.execute(
                    "INSERT INTO items (source_type, source_id, content, title) "
                    "VALUES (?, ?, ?, ?)",
                    ("test", "peer-item", "peer content", "Peer Title"),
                )
                conn_peer.commit()
                changes = crsqlite.get_changes_since(conn_peer, since_version=0)

        # 3. Push changes to server
        response = client.post("/sync/changes", json={"changes": _serialize_changes(changes)})
        apply_result = response.json()
        assert apply_result["applied"] > 0

        # 4. Verify changes are retrievable via GET
        get_result = client.get("/sync/changes", params={"since": initial_version}).json()
        assert len(get_result["changes"]) > 0
        tables_changed = {c["table"] for c in get_result["changes"]}
        assert "items" in tables_changed

        # 5. Verify data is in the database
        with get_crdt_connection(config) as conn:
            cursor = conn.execute("SELECT title FROM items WHERE source_id = ?", ("peer-item",))
            assert cursor.fetchone()[0] == "Peer Title"

    def test_concurrent_inserts_merge(self, sync_server):
        """Both server and peer insert items, then changes merge correctly."""
        client, config = sync_server

        # Insert on server with explicit ID to avoid auto-increment PK collision
        with get_crdt_connection(config) as conn:
            conn.execute(
                "INSERT INTO items (id, source_type, source_id, content) VALUES (?, ?, ?, ?)",
                (1000, "test", "server-item", "server content"),
            )
            conn.commit()

        # Insert on peer with non-colliding explicit ID and push
        with tempfile.TemporaryDirectory() as tmpdir:
            config_peer = _create_peer_db(tmpdir)

            with get_crdt_connection(config_peer) as conn_peer:
                conn_peer.execute(
                    "INSERT INTO items (id, source_type, source_id, content) VALUES (?, ?, ?, ?)",
                    (2000, "test", "peer-item", "peer content"),
                )
                conn_peer.commit()
                changes = crsqlite.get_changes_since(conn_peer, since_version=0)

        response = client.post("/sync/changes", json={"changes": _serialize_changes(changes)})
        assert response.status_code == 200
        assert response.json()["applied"] > 0

        # Both items should exist on server
        with get_crdt_connection(config) as conn:
            cursor = conn.execute("SELECT source_id FROM items ORDER BY source_id")
            source_ids = [row[0] for row in cursor.fetchall()]
            assert "server-item" in source_ids
            assert "peer-item" in source_ids

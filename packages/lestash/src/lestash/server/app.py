"""HTTP sync server for Le Stash.

Provides endpoints for cr-sqlite CRDT sync with the mobile app.
"""

import contextlib
import logging
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel

from lestash.cli.sync import SYNC_TABLES
from lestash.core import crsqlite
from lestash.core.config import Config
from lestash.core.database import SCHEMA_VERSION, get_crdt_connection

logger = logging.getLogger(__name__)


class ProtocolInfo(BaseModel):
    version: int
    format: str
    schema_version: int
    crsqlite_version: str
    sync_tables: list[str]


class SyncStatusResponse(BaseModel):
    site_id: str
    db_version: int
    protocol: ProtocolInfo


class ChangesResponse(BaseModel):
    changes: list[dict[str, Any]]


class ApplyChangesRequest(BaseModel):
    changes: list[dict[str, Any]]


class ApplyChangesResponse(BaseModel):
    applied: int
    db_version: int


def _serialize_change(change: dict[str, Any]) -> dict[str, Any]:
    """Ensure all change record values are JSON-serializable.

    cr-sqlite returns `pk` as bytes. Convert bytes to hex strings
    for JSON transport, matching the existing `site_id` hex convention.
    """
    result = dict(change)
    for key, value in result.items():
        if isinstance(value, bytes):
            result[key] = value.hex()
    return result


def _deserialize_change(change: dict[str, Any]) -> dict[str, Any]:
    """Convert hex-encoded `pk` back to bytes for cr-sqlite.

    The `site_id` hex-to-bytes conversion is already handled by
    crsqlite.apply_changes(). This handles `pk` which is also bytes.
    """
    result = dict(change)
    if "pk" in result and isinstance(result["pk"], str):
        with contextlib.suppress(ValueError):
            result["pk"] = bytes.fromhex(result["pk"])
    return result


def create_app(config: Config | None = None) -> FastAPI:
    """Create the FastAPI sync server application.

    Args:
        config: Le Stash config. If None, loads from default location.
    """
    if config is None:
        config = Config.load()

    app = FastAPI(
        title="Le Stash Sync Server",
        version="1.0.0",
    )

    @app.get("/sync/status", response_model=SyncStatusResponse)
    def sync_status():
        with get_crdt_connection(config) as conn:
            site_id = crsqlite.get_site_id(conn)
            db_version = crsqlite.get_db_version(conn)

        return SyncStatusResponse(
            site_id=site_id.hex() if site_id else "",
            db_version=db_version,
            protocol=ProtocolInfo(
                version=1,
                format="lestash-crsqlite-v1",
                schema_version=SCHEMA_VERSION,
                crsqlite_version=crsqlite.CRSQLITE_VERSION,
                sync_tables=list(SYNC_TABLES),
            ),
        )

    @app.get("/sync/changes", response_model=ChangesResponse)
    def sync_changes(since: int = Query(default=0)):
        with get_crdt_connection(config) as conn:
            changes = crsqlite.get_changes_since(conn, since_version=since)
        return ChangesResponse(changes=[_serialize_change(c) for c in changes])

    @app.post("/sync/changes", response_model=ApplyChangesResponse)
    def apply_changes(request: ApplyChangesRequest):
        deserialized = [_deserialize_change(c) for c in request.changes]
        with get_crdt_connection(config) as conn:
            applied = crsqlite.apply_changes(conn, deserialized)
            if applied > 0:
                crsqlite.rebuild_fts_index(conn)
            db_version = crsqlite.get_db_version(conn)
        return ApplyChangesResponse(applied=applied, db_version=db_version)

    return app

"""Source API endpoints."""

import json
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException
from lestash.core.database import get_connection
from lestash.plugins.loader import load_plugins

from lestash_server.deps import get_config, get_db
from lestash_server.models import SourceResponse, SyncLogEntry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("", response_model=list[SourceResponse])
def list_sources():
    """List installed source plugins with sync status."""
    plugins = load_plugins()
    results = []

    with get_db() as conn:
        for name, plugin in plugins.items():
            row = conn.execute(
                "SELECT enabled, last_sync FROM sources WHERE source_type = ?",
                (name,),
            ).fetchone()

            results.append(
                SourceResponse(
                    name=name,
                    description=plugin.description,
                    enabled=bool(row["enabled"]) if row else True,
                    last_sync=row["last_sync"] if row else None,
                )
            )

    return results


@router.get("/status", response_model=list[SyncLogEntry])
def source_status():
    """Show recent sync history."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT source_type, started_at, completed_at, status,
                   items_added, items_updated, error_message
            FROM sync_log
            ORDER BY started_at DESC
            LIMIT 20
            """
        ).fetchall()

    return [SyncLogEntry(**dict(row)) for row in rows]


def _run_sync(source_name: str) -> None:
    """Run a sync in the background."""
    plugins = load_plugins()
    plugin = plugins.get(source_name)
    if not plugin:
        return

    config = get_config()
    plugin_config = config.get_plugin_config(source_name)

    with get_connection(config) as conn:
        started_at = datetime.now()
        cursor = conn.execute(
            "INSERT INTO sync_log (source_type, started_at, status) VALUES (?, ?, ?)",
            (source_name, started_at, "running"),
        )
        sync_id = cursor.lastrowid
        conn.commit()

        items_added = 0
        error_message = None

        try:
            for item in plugin.sync(plugin_config):
                metadata_json = json.dumps(item.metadata) if item.metadata else None
                conn.execute(
                    """
                    INSERT INTO items (
                        source_type, source_id, url, title, content,
                        author, created_at, is_own_content, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id) DO UPDATE SET
                        url = excluded.url,
                        title = excluded.title,
                        content = excluded.content,
                        author = excluded.author,
                        is_own_content = excluded.is_own_content,
                        metadata = excluded.metadata
                    """,
                    (
                        item.source_type,
                        item.source_id,
                        item.url,
                        item.title,
                        item.content,
                        item.author,
                        item.created_at,
                        item.is_own_content,
                        metadata_json,
                    ),
                )
                items_added += 1

            conn.commit()

            conn.execute(
                """
                INSERT INTO sources (source_type, last_sync)
                VALUES (?, ?)
                ON CONFLICT(source_type) DO UPDATE SET last_sync = excluded.last_sync
                """,
                (source_name, datetime.now()),
            )
            conn.commit()
            status = "completed"
        except Exception as e:
            status = "failed"
            error_message = str(e)
            logger.error(f"Sync failed for {source_name}: {e}")

        conn.execute(
            """
            UPDATE sync_log SET
                completed_at = ?, status = ?,
                items_added = ?, items_updated = ?,
                error_message = ?
            WHERE id = ?
            """,
            (datetime.now(), status, items_added, 0, error_message, sync_id),
        )
        conn.commit()


@router.post("/{source_name}/sync")
def sync_source(source_name: str, background_tasks: BackgroundTasks):
    """Trigger a sync for a source plugin."""
    plugins = load_plugins()
    if source_name not in plugins:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name}")

    background_tasks.add_task(_run_sync, source_name)
    return {"status": "started", "source": source_name}

"""Embeddings API endpoints — status, rebuild, similar items."""

import logging
import threading

from fastapi import APIRouter, HTTPException
from lestash.core.embeddings import (
    LOG_SOURCE,
    embed_query,
    ensure_vec_table,
    get_embedding_stats,
    load_vec_extension,
    make_embed_text,
    rebuild_embeddings,
    search_similar,
)
from lestash.models.item import Item
from pydantic import BaseModel

from lestash_server.deps import get_config, get_db
from lestash_server.enrich import enrich_item
from lestash_server.models import ItemResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])

_rebuild_lock = threading.Lock()
_rebuild_running = False


class EmbeddingStatus(BaseModel):
    model: str
    recipe: str
    dimensions: int
    query_model: str
    building: bool
    embedded: int
    parts_embedded: int
    out_of_date: int
    total_parents: int
    coverage: str
    rebuilding: bool = False


class RebuildResponse(BaseModel):
    status: str
    message: str


@router.get("/status", response_model=EmbeddingStatus)
def embedding_status():
    """Get embedding coverage statistics."""
    with get_db() as conn:
        load_vec_extension(conn)
        ensure_vec_table(conn)
        stats = get_embedding_stats(conn)
    return EmbeddingStatus(**stats, rebuilding=_rebuild_running)


@router.post("/rebuild", response_model=RebuildResponse)
def trigger_rebuild():
    """Trigger embedding rebuild in background thread."""
    global _rebuild_running  # noqa: PLW0603

    # Taken here, released by the thread: a second request never waits behind it
    if not _rebuild_lock.acquire(blocking=False):
        return RebuildResponse(status="already_running", message="Rebuild is already in progress")
    _rebuild_running = True

    config = get_config()  # the server's database, read before the thread starts
    # Set once the thread holds the sync log's lock, or has given up
    took_lock = threading.Event()
    outcome = {"status": "starting"}  # still starting after the wait: answer started

    def _do_rebuild():
        global _rebuild_running  # noqa: PLW0603
        try:
            from lestash.core.database import get_connection
            from lestash.core.sync import SyncAlreadyRunning, SyncRun

            with get_connection(config) as conn:
                load_vec_extension(conn)
                try:
                    # Logged with the syncs, so Settings → Sources shows how it ended
                    run = SyncRun.start(conn, LOG_SOURCE)
                except SyncAlreadyRunning:  # a rebuild from the CLI holds it
                    outcome["status"] = "already_running"
                    return
                outcome["status"] = "started"
                took_lock.set()
                with run:
                    count = rebuild_embeddings(conn)
                    run.finish("completed", items_added=count, items_updated=0)
                logger.info(f"Rebuild complete: {count} items embedded")
        except Exception:
            logger.exception("Embedding rebuild failed")
            if outcome["status"] == "starting":
                outcome["status"] = "failed"
        finally:
            _rebuild_running = False
            _rebuild_lock.release()
            took_lock.set()

    thread = threading.Thread(target=_do_rebuild, daemon=True)
    thread.start()
    took_lock.wait(timeout=30)
    if outcome["status"] == "already_running":
        return RebuildResponse(status="already_running", message="Rebuild is already in progress")
    if outcome["status"] == "failed":
        return RebuildResponse(status="failed", message="Rebuild could not start; see the log")

    return RebuildResponse(status="started", message="Rebuild started in background")


@router.get("/similar/{item_id}", response_model=list[ItemResponse])
def find_similar(item_id: int, limit: int = 10):
    """Find items semantically similar to a given item."""
    with get_db() as conn:
        load_vec_extension(conn)
        ensure_vec_table(conn)

        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Item not found")

        item = Item.from_row(row)
        text = make_embed_text(item)
        if not text:
            return []

        query_emb = embed_query(conn, text)
        # Parts have vectors too (#221): ask for more, then keep whole items outside
        # this item's own family, as related items compare whole items
        family = item.parent_id if item.parent_id is not None else item_id
        results = search_similar(conn, query_emb, limit=limit * 8 + 1)

        items = []
        for rid, _distance in results:
            if rid == family:
                continue
            r = conn.execute(
                "SELECT * FROM items WHERE id = ? AND parent_id IS NULL", (rid,)
            ).fetchone()
            if r:
                items.append(enrich_item(conn, Item.from_row(r)))
            if len(items) >= limit:
                break

    return items

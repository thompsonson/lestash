"""Embeddings API endpoints — status, rebuild, similar items."""

import logging
import threading

from fastapi import APIRouter, HTTPException
from lestash.core.embeddings import (
    embed_text,
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
    embedded: int
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

    if _rebuild_running:
        return RebuildResponse(status="already_running", message="Rebuild is already in progress")

    config = get_config()  # the server's database, read before the thread starts

    def _do_rebuild():
        global _rebuild_running  # noqa: PLW0603
        with _rebuild_lock:
            _rebuild_running = True
            try:
                from lestash.core.database import get_connection

                with get_connection(config) as conn:
                    load_vec_extension(conn)
                    count = rebuild_embeddings(conn)
                    logger.info(f"Rebuild complete: {count} items embedded")
            except Exception:
                logger.exception("Embedding rebuild failed")
            finally:
                _rebuild_running = False

    thread = threading.Thread(target=_do_rebuild, daemon=True)
    thread.start()

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

        query_emb = embed_text(text)
        results = search_similar(conn, query_emb, limit=limit + 1)

        items = []
        for rid, _distance in results:
            if rid == item_id:
                continue
            r = conn.execute("SELECT * FROM items WHERE id = ?", (rid,)).fetchone()
            if r:
                items.append(enrich_item(conn, Item.from_row(r)))
            if len(items) >= limit:
                break

    return items

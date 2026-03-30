"""Embeddings API endpoints — status, rebuild, similar items."""

import logging
import threading

from fastapi import APIRouter, HTTPException
from lestash.core.database import get_tags
from lestash.core.embeddings import (
    embed_text,
    ensure_vec_table,
    get_embedding_stats,
    load_vec_extension,
    rebuild_embeddings,
    search_similar,
)
from lestash.core.enrichment import get_author_actor, get_item_subtype, get_preview
from lestash.models.item import Item
from pydantic import BaseModel

from lestash_server.deps import get_db
from lestash_server.models import ItemResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/embeddings", tags=["embeddings"])

_rebuild_lock = threading.Lock()
_rebuild_running = False


class EmbeddingStatus(BaseModel):
    model: str
    dimensions: int
    embedded: int
    total_parents: int
    coverage: str
    rebuilding: bool = False


class RebuildResponse(BaseModel):
    status: str
    message: str


def _enrich_item(conn, item: Item) -> ItemResponse:
    """Convert an Item to an enriched API response."""
    author_display, actor_display = get_author_actor(conn, item)
    child_count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE parent_id = ?", (item.id,)
    ).fetchone()[0]
    return ItemResponse(
        id=item.id,
        source_type=item.source_type,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        content=item.content,
        author=item.author,
        created_at=item.created_at,
        fetched_at=item.fetched_at,
        is_own_content=item.is_own_content,
        metadata=item.metadata,
        parent_id=item.parent_id,
        subtype=get_item_subtype(item),
        author_display=author_display,
        actor_display=actor_display,
        preview=get_preview(conn, item, max_length=120),
        tags=get_tags(conn, item.id),
        child_count=child_count,
    )


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

    def _do_rebuild():
        global _rebuild_running  # noqa: PLW0603
        with _rebuild_lock:
            _rebuild_running = True
            try:
                from lestash.core.database import get_connection

                with get_connection() as conn:
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
        text = f"{item.title or ''} {(item.content or '')[:500]}".strip()
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
                items.append(_enrich_item(conn, Item.from_row(r)))
            if len(items) >= limit:
                break

    return items

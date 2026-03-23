"""Item API endpoints."""

from fastapi import APIRouter, HTTPException, Query
from lestash.core.enrichment import get_author_actor, get_item_subtype, get_preview
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.models import ItemListResponse, ItemResponse

router = APIRouter(prefix="/api/items", tags=["items"])


def _enrich_item(conn, item: Item) -> ItemResponse:
    """Convert an Item to an enriched API response."""
    author_display, actor_display = get_author_actor(conn, item)
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
        subtype=get_item_subtype(item),
        author_display=author_display,
        actor_display=actor_display,
        preview=get_preview(conn, item, max_length=120),
    )


@router.get("", response_model=ItemListResponse)
def list_items(
    source: str | None = Query(None, description="Filter by source type"),
    own: bool | None = Query(None, description="Filter own content"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List items with optional filters."""
    with get_db() as conn:
        # Build count query
        count_query = "SELECT COUNT(*) FROM items WHERE 1=1"
        query = "SELECT * FROM items WHERE 1=1"
        params: list = []

        if source:
            query += " AND source_type = ?"
            count_query += " AND source_type = ?"
            params.append(source)

        if own is not None:
            query += " AND is_own_content = ?"
            count_query += " AND is_own_content = ?"
            params.append(own)

        total = conn.execute(count_query, params).fetchone()[0]

        query += " ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        items = [_enrich_item(conn, Item.from_row(row)) for row in rows]

    return ItemListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", response_model=ItemListResponse)
def search_items(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
):
    """Full-text search using FTS5."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT items.* FROM items
            JOIN items_fts ON items.id = items_fts.rowid
            WHERE items_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()

        items = [_enrich_item(conn, Item.from_row(row)) for row in rows]

    return ItemListResponse(items=items, total=len(items), limit=limit, offset=0)


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: int):
    """Get a single item by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return _enrich_item(conn, Item.from_row(row))

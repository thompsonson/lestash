"""Item API endpoints."""

import json

from fastapi import APIRouter, HTTPException, Query
from lestash.core.enrichment import get_author_actor, get_item_subtype, get_preview
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.models import ItemCreateRequest, ItemListResponse, ItemResponse

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


def _matches_exclude(subtype: str, excludes: set[str]) -> bool:
    """Check if a subtype matches any exclude term."""
    return any(ex in subtype for ex in excludes)


@router.get("", response_model=ItemListResponse)
def list_items(
    source: str | None = Query(None, description="Filter by source type"),
    own: bool | None = Query(None, description="Filter own content"),
    exclude_subtype: str | None = Query(
        None, description="Comma-separated subtypes to exclude (e.g., reaction,invitation,message)"
    ),
    since: str | None = Query(None, description="Only items fetched since this ISO datetime"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List items with optional filters."""
    with get_db() as conn:
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

        if since:
            query += " AND datetime(fetched_at) >= datetime(?)"
            count_query += " AND datetime(fetched_at) >= datetime(?)"
            params.append(since)

        total = conn.execute(count_query, params).fetchone()[0]

        # Sort by fetched_at when filtering recent, otherwise by created_at
        sort_col = "fetched_at" if since else "created_at"
        query += f" ORDER BY datetime({sort_col}) DESC"

        excludes = set()
        if exclude_subtype:
            excludes = {s.strip() for s in exclude_subtype.split(",") if s.strip()}

        if excludes:
            # Fetch extra rows to account for filtered-out items
            fetch_limit = limit * 4
            query += " LIMIT ? OFFSET ?"
            params.extend([fetch_limit, offset])

            rows = conn.execute(query, params).fetchall()
            all_enriched = [_enrich_item(conn, Item.from_row(row)) for row in rows]
            items = [i for i in all_enriched if not _matches_exclude(i.subtype, excludes)]

            # Adjust total to reflect filtering (approximate)
            if all_enriched:
                filter_ratio = len(items) / len(all_enriched)
                total = int(total * filter_ratio)

            items = items[:limit]
        else:
            query += " LIMIT ? OFFSET ?"
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


@router.post("", response_model=ItemResponse, status_code=201)
def create_item(body: ItemCreateRequest):
    """Create a single item."""
    metadata_json = json.dumps(body.metadata) if body.metadata else None
    source_id = body.source_id or body.url

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                content = excluded.content,
                title = excluded.title,
                author = excluded.author,
                metadata = excluded.metadata
            """,
            (
                body.source_type,
                source_id,
                body.url,
                body.title,
                body.content,
                body.author,
                body.created_at,
                body.is_own_content,
                metadata_json,
            ),
        )
        conn.commit()

        item_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            # Fetch by source_type + source_id if lastrowid didn't work (upsert case)
            row = conn.execute(
                "SELECT * FROM items WHERE source_type = ? AND source_id = ?",
                (body.source_type, source_id),
            ).fetchone()
        return _enrich_item(conn, Item.from_row(row))


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: int):
    """Get a single item by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return _enrich_item(conn, Item.from_row(row))

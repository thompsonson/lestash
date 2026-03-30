"""Collection API endpoints."""

from fastapi import APIRouter, HTTPException
from lestash.core.database import get_tags
from lestash.core.enrichment import get_author_actor, get_item_subtype, get_preview
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.models import (
    CollectionCreate,
    CollectionDetailResponse,
    CollectionItemAdd,
    CollectionResponse,
    ItemResponse,
)

router = APIRouter(prefix="/api/collections", tags=["collections"])


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


@router.get("", response_model=list[CollectionResponse])
def list_collections():
    """List all collections with item counts."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.description, c.created_at,
                   COUNT(ci.item_id) as item_count
            FROM collections c
            LEFT JOIN collection_items ci ON c.id = ci.collection_id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
        return [
            CollectionResponse(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                item_count=r["item_count"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


@router.post("", response_model=CollectionResponse, status_code=201)
def create_collection(body: CollectionCreate):
    """Create a new collection."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO collections (name, description) VALUES (?, ?)",
            (body.name, body.description),
        )
        conn.commit()
        return CollectionResponse(
            id=cursor.lastrowid or 0,
            name=body.name,
            description=body.description,
            item_count=0,
        )


@router.get("/{collection_id}", response_model=CollectionDetailResponse)
def get_collection(collection_id: int):
    """Get a collection with all its items."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Collection not found")

        item_rows = conn.execute(
            """
            SELECT items.* FROM items
            JOIN collection_items ci ON items.id = ci.item_id
            WHERE ci.collection_id = ?
            ORDER BY ci.added_at DESC
            """,
            (collection_id,),
        ).fetchall()

        items = [_enrich_item(conn, Item.from_row(r)) for r in item_rows]

        return CollectionDetailResponse(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            items=items,
            created_at=row["created_at"],
        )


@router.put("/{collection_id}", response_model=CollectionResponse)
def update_collection(collection_id: int, body: CollectionCreate):
    """Update a collection's name and description."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Collection not found")

        conn.execute(
            "UPDATE collections SET name = ?, description = ?,"
            " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (body.name, body.description, collection_id),
        )
        conn.commit()

        item_count = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()[0]

        return CollectionResponse(
            id=collection_id,
            name=body.name,
            description=body.description,
            item_count=item_count,
        )


@router.delete("/{collection_id}", status_code=204)
def delete_collection(collection_id: int):
    """Delete a collection (items are not deleted)."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Collection not found")
        conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        conn.commit()


@router.post("/{collection_id}/items", response_model=CollectionResponse, status_code=201)
def add_item_to_collection(collection_id: int, body: CollectionItemAdd):
    """Add an item to a collection."""
    with get_db() as conn:
        coll = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if not coll:
            raise HTTPException(status_code=404, detail="Collection not found")

        item = conn.execute("SELECT id FROM items WHERE id = ?", (body.item_id,)).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        conn.execute(
            "INSERT OR IGNORE INTO collection_items"
            " (collection_id, item_id, note) VALUES (?, ?, ?)",
            (collection_id, body.item_id, body.note),
        )
        conn.execute(
            "UPDATE collections SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (collection_id,),
        )
        conn.commit()

        item_count = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE collection_id = ?",
            (collection_id,),
        ).fetchone()[0]

        return CollectionResponse(
            id=collection_id,
            name=coll["name"],
            description=coll["description"],
            item_count=item_count,
            created_at=coll["created_at"],
        )


@router.delete("/{collection_id}/items/{item_id}", status_code=204)
def remove_item_from_collection(collection_id: int, item_id: int):
    """Remove an item from a collection."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?",
            (collection_id, item_id),
        )
        conn.execute(
            "UPDATE collections SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (collection_id,),
        )
        conn.commit()

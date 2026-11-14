"""Collection API endpoints."""

import sqlite3

from fastapi import APIRouter, HTTPException
from lestash.core.collections import KOBO, ORDER_SQL, add_item, kobo_status, move_item, set_shown
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.enrich import enrich_item
from lestash_server.models import (
    CollectionCreate,
    CollectionDetailResponse,
    CollectionItemAdd,
    CollectionItemMove,
    CollectionKobo,
    CollectionKoboSwitch,
    CollectionResponse,
)

router = APIRouter(prefix="/api/collections", tags=["collections"])


def _kobo(conn: sqlite3.Connection, collection_id: int) -> CollectionKobo | None:
    st = kobo_status(conn, collection_id)
    if st is None:
        return None
    return CollectionKobo(
        shown=st.shown,
        on=st.on,
        waiting=st.waiting,
        missing=st.missing,
        status=st.text(),
        last_written=st.last_written,
        error=st.error,
    )


def _summary(conn: sqlite3.Connection, coll: sqlite3.Row) -> CollectionResponse:
    item_count = conn.execute(
        "SELECT COUNT(*) FROM collection_items WHERE collection_id = ?", (coll["id"],)
    ).fetchone()[0]
    return CollectionResponse(
        id=coll["id"],
        name=coll["name"],
        description=coll["description"],
        item_count=item_count,
        created_at=coll["created_at"],
        kobo=_kobo(conn, coll["id"]),
    )


def _collection(conn: sqlite3.Connection, collection_id: int) -> sqlite3.Row:
    coll = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")
    return coll


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
                kobo=_kobo(conn, r["id"]),
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
            f"""
            SELECT items.* FROM items
            JOIN collection_items ci ON items.id = ci.item_id
            WHERE ci.collection_id = ?
            ORDER BY {ORDER_SQL}
            """,
            (collection_id,),
        ).fetchall()

        items = [enrich_item(conn, Item.from_row(r)) for r in item_rows]

        return CollectionDetailResponse(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            items=items,
            created_at=row["created_at"],
            kobo=_kobo(conn, collection_id),
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

        add_item(conn, collection_id, body.item_id, body.note)
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


@router.put("/{collection_id}/items/{item_id}", response_model=CollectionResponse)
def move_collection_item(collection_id: int, item_id: int, body: CollectionItemMove):
    """Move an item in the collection's reading order (#317); 1 is first."""
    with get_db() as conn:
        coll = _collection(conn, collection_id)
        if not move_item(conn, collection_id, item_id, body.position):
            raise HTTPException(status_code=404, detail="Item not in this collection")
        conn.execute(
            "UPDATE collections SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (collection_id,),
        )
        conn.commit()
        return _summary(conn, coll)


@router.put("/{collection_id}/kobo", response_model=CollectionResponse)
def show_on_kobo(collection_id: int, body: CollectionKoboSwitch):
    """Show the collection on the Kobo, or take it off, from the next plug-in (#315)."""
    with get_db() as conn:
        coll = _collection(conn, collection_id)
        set_shown(conn, collection_id, KOBO, body.shown)
        conn.commit()
        return _summary(conn, coll)

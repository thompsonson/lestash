"""Item API endpoints."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from lestash.core.agents import agents_for
from lestash.core.database import (
    add_tag,
    get_item_media,
    get_tags,
    list_tags,
    mark_recent_history,
    max_history_id,
    remove_tag,
)
from lestash.core.embeddings import re_embed_item
from lestash.core.enrichment import (
    get_author_actor,
    get_item_subtype,
    get_preview,
    subtype_excludes_sql,
)
from lestash.core.links import links_of, reject_link
from lestash.core.search import keyword_search
from lestash.models.item import Item

from lestash_server.deps import get_db
from lestash_server.models import (
    _UNSET,
    Activity,
    ActivityListResponse,
    HistoryListResponse,
    HistoryVersion,
    HistoryVersionDetail,
    ItemAgent,
    ItemAgentListResponse,
    ItemCreateRequest,
    ItemLink,
    ItemLinkListResponse,
    ItemListResponse,
    ItemPatchRequest,
    ItemResponse,
    MediaResponse,
    TagAddRequest,
    TagListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/items", tags=["items"])


def _media_responses(conn, item_id: int) -> list[MediaResponse]:
    """Build MediaResponse list for an item."""
    return [
        MediaResponse(
            id=m["id"],
            media_type=m["media_type"],
            url=m["url"],
            serve_url=f"/api/media/{m['id']}",
            alt_text=m["alt_text"],
            position=m["position"],
            available=bool(
                m["sha256"] or m["local_path"] or (m["url"] and m["url"].startswith("http"))
            ),
            mime_type=m["mime_type"],
            version=m["version"],
            superseded_at=m["superseded_at"],
        )
        for m in get_item_media(conn, item_id)
    ]


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
        held=item.held,
        subtype=get_item_subtype(item),
        author_display=author_display,
        actor_display=actor_display,
        preview=get_preview(conn, item, max_length=120),
        tags=get_tags(conn, item.id),
        child_count=child_count,
        media=_media_responses(conn, item.id),
    )


@router.get("", response_model=ItemListResponse)
def list_items(
    source: str | None = Query(None, description="Filter by source type"),
    own: bool | None = Query(None, description="Filter own content"),
    exclude_subtype: str | None = Query(
        None, description="Comma-separated subtypes to exclude (e.g., reaction,invitation,message)"
    ),
    since: str | None = Query(None, description="Only items fetched since this ISO datetime"),
    tag: str | None = Query(None, description="Filter by tag name"),
    parent_id: int | None = Query(None, description="Filter to children of this parent item"),
    include_children: bool = Query(False, description="Include items that have a parent"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List items with optional filters.

    Every filter runs in SQL before LIMIT, so each page is full and `total` is exact.
    """
    where: list[str] = []
    params: list = []
    joins = ""

    if parent_id is not None:
        where.append("items.parent_id = ?")
        params.append(parent_id)
    else:
        # A tag lists whatever carries it, children included, as it always has
        if not include_children and not tag:
            where.append("items.parent_id IS NULL")
        # Stubs are known by their address only; their detail view shows them
        where.append("items.held = 1")

    if tag:
        joins = (
            " JOIN item_tags ON items.id = item_tags.item_id"
            " JOIN tags ON item_tags.tag_id = tags.id"
        )
        where.append("tags.name = ?")
        params.append(tag.strip().lower())

    if source:
        where.append("items.source_type = ?")
        params.append(source)

    if own is not None:
        where.append("items.is_own_content = ?")
        params.append(own)

    if since:
        where.append("datetime(items.fetched_at) >= datetime(?)")
        params.append(since)

    excludes = sorted({s.strip() for s in (exclude_subtype or "").split(",") if s.strip()})
    if excludes:
        clause, clause_params = subtype_excludes_sql(excludes)
        where.append(clause)
        params.extend(clause_params)

    body = f"FROM items{joins}" + (" WHERE " + " AND ".join(where) if where else "")
    # Sort by fetched_at when filtering recent, otherwise by created_at
    sort_col = "fetched_at" if since else "created_at"

    with get_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) {body}", params).fetchone()[0]
        # Ties go to insertion order: a chat's reply, stored after its prompt with the
        # same time, sorts after it
        rows = conn.execute(
            f"SELECT items.* {body} ORDER BY datetime(items.{sort_col}) DESC, items.id DESC"
            " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = [_enrich_item(conn, Item.from_row(row)) for row in rows]

    return ItemListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/search", response_model=ItemListResponse)
def search_items(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    include_children: bool = Query(True, description="Include child items in results"),
    mode: Literal["keyword", "semantic", "hybrid"] = Query("hybrid", description="Search mode"),
):
    """Search items using keyword (FTS5), semantic (vector), or hybrid mode.

    Supports "phrase search", -"excluded phrase", prefix* and AND, OR, NOT; other
    punctuation is plain text
    (see lestash.core.search).
    """
    with get_db() as conn:
        fts_results: dict[int, dict] = {}
        vec_results: dict[int, float] = {}
        keyword_failed = False

        # FTS5 keyword search
        if mode in ("keyword", "hybrid"):
            try:
                rows = keyword_search(
                    conn, q, limit * 2, top_level_only=not include_children, snippets=True
                )
                for rank, row in enumerate(rows):
                    fts_results[row["id"]] = {
                        "row": row,
                        "rank": rank,
                        "snippet": row["search_snippet"],
                    }
            except sqlite3.Error:
                # keyword_search leaves no syntax errors, so this is the index or the
                # database (e.g. a damaged items_fts). Log it; hybrid still returns the
                # semantic results, keyword mode reports it.
                logger.warning("Keyword search failed for %r", q, exc_info=True)
                keyword_failed = True
                if mode == "keyword":
                    raise HTTPException(
                        status_code=500, detail="Keyword search failed; see the server log"
                    ) from None

        # Vector semantic search
        if mode in ("semantic", "hybrid"):
            try:
                from lestash.core.embeddings import (
                    embed_text,
                    ensure_vec_table,
                    load_vec_extension,
                    search_similar,
                )

                load_vec_extension(conn)
                ensure_vec_table(conn)
                query_emb = embed_text(q)
                # Children are filtered out after the vector search, so ask for more
                # to still fill the page with top-level items
                want = limit * 2
                fetch = want if include_children else want * 4
                similar = search_similar(conn, query_emb, limit=fetch)
                ids = [item_id for item_id, _distance in similar]
                if not include_children and ids:
                    marks = ",".join("?" * len(ids))
                    top = {
                        r[0]
                        for r in conn.execute(
                            f"SELECT id FROM items WHERE id IN ({marks}) AND parent_id IS NULL",
                            ids,
                        )
                    }
                    ids = [i for i in ids if i in top]
                for rank, item_id in enumerate(ids[:want]):
                    vec_results[item_id] = rank
            except Exception:
                logger.warning("Vector search failed for %r", q, exc_info=True)
                # Semantic mode has nothing else to show; hybrid with a failed keyword
                # half is caught by the check below
                if mode == "semantic":
                    raise HTTPException(
                        status_code=500, detail="Search unavailable; see the server log"
                    ) from None

        if keyword_failed and not vec_results:
            # Keyword search failed and semantic found nothing: an empty 200 would read as
            # "no results" when the search never ran properly
            raise HTTPException(status_code=500, detail="Search unavailable; see the server log")

        # Merge results via RRF (Reciprocal Rank Fusion)
        k = 60  # RRF constant
        scores: dict[int, float] = {}
        all_ids = set(fts_results.keys()) | set(vec_results.keys())

        for item_id in all_ids:
            score = 0.0
            if item_id in fts_results:
                score += 1.0 / (k + fts_results[item_id]["rank"])
            if item_id in vec_results:
                score += 1.0 / (k + vec_results[item_id])
            scores[item_id] = score

        ranked_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

        # Build response
        items = []
        for item_id in ranked_ids:
            if item_id in fts_results:
                row = fts_results[item_id]["row"]
                item = Item.from_row(row)
                enriched = _enrich_item(conn, item)
                snippet = fts_results[item_id]["snippet"]
                if snippet:
                    enriched.preview = snippet
            else:
                row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    continue
                item = Item.from_row(row)
                enriched = _enrich_item(conn, item)
            items.append(enriched)

    return ItemListResponse(items=items, total=len(items), limit=limit, offset=0)


@router.post("", response_model=ItemResponse, status_code=201)
def create_item(body: ItemCreateRequest):
    """Create a single item."""
    metadata_json = json.dumps(body.metadata) if body.metadata else None
    source_id = body.source_id or body.url
    created_at = body.created_at or datetime.now(UTC)

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata, parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                content = excluded.content,
                title = excluded.title,
                author = excluded.author,
                metadata = excluded.metadata,
                parent_id = excluded.parent_id
            """,
            (
                body.source_type,
                source_id,
                body.url,
                body.title,
                body.content,
                body.author,
                created_at,
                body.is_own_content,
                metadata_json,
                body.parent_id,
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


@router.get("/tags", response_model=TagListResponse)
def get_all_tags():
    """List all tags with item counts."""
    from lestash_server.models import TagInfo

    with get_db() as conn:
        return TagListResponse(
            tags=[TagInfo(**t) for t in list_tags(conn)],
        )


@router.patch("/{item_id}", response_model=ItemResponse)
def update_item(item_id: int, body: ItemPatchRequest):
    """Partially update an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        if not row["held"]:
            # The stub is merged into the thing itself once imported; an edit would be lost
            raise HTTPException(status_code=409, detail=f"Item {item_id} is a stub")

        updates: list[str] = []
        params: list = []

        if body.content is not None:
            updates.append("content = ?")
            params.append(body.content)

        if body.title is not _UNSET:
            updates.append("title = ?")
            params.append(body.title)

        if body.parent_id is not _UNSET:
            new_parent = body.parent_id
            if new_parent is not None:
                if new_parent == item_id:
                    raise HTTPException(status_code=400, detail="Item cannot be its own parent")
                # Check parent exists
                if not conn.execute("SELECT 1 FROM items WHERE id = ?", (new_parent,)).fetchone():
                    raise HTTPException(
                        status_code=400, detail=f"Parent item {new_parent} not found"
                    )
                # Check for circular reference
                ancestor = new_parent
                while ancestor is not None:
                    parent_row = conn.execute(
                        "SELECT parent_id FROM items WHERE id = ?", (ancestor,)
                    ).fetchone()
                    if parent_row is None:
                        break
                    ancestor = parent_row[0]
                    if ancestor == item_id:
                        raise HTTPException(
                            status_code=400,
                            detail="Circular parent reference detected",
                        )
            updates.append("parent_id = ?")
            params.append(new_parent)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Only re-embed when title or content actually differ from current row,
        # not just because the client sent the field with the same value.
        text_changed = (body.content is not None and body.content != row["content"]) or (
            body.title is not _UNSET and body.title != row["title"]
        )
        pre_max = max_history_id(conn)

        params.append(item_id)
        conn.execute(
            f"UPDATE items SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        mark_recent_history(conn, pre_max, "user-edit", item_ids=[item_id])
        conn.commit()

        if text_changed:
            re_embed_item(conn, item_id)

        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _enrich_item(conn, Item.from_row(row))


def _content_preview(content: str | None, max_length: int = 200) -> str | None:
    if content is None:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:max_length] + ("…" if len(stripped) > max_length else "")
    return None


def _json_object(text: str | None) -> dict | None:
    """A stored JSON object, or None when the text is empty or not an object."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


@router.get("/{item_id}/history", response_model=HistoryListResponse)
def list_item_history(item_id: int):
    """List all history versions for an item, newest first."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        rows = conn.execute(
            """SELECT id, changed_at, change_reason, change_type,
                      title_old, content_old, parent_id_old
               FROM item_history WHERE item_id = ? ORDER BY id DESC""",
            (item_id,),
        ).fetchall()

        versions = [
            HistoryVersion(
                id=r["id"],
                changed_at=r["changed_at"],
                change_reason=r["change_reason"],
                change_type=r["change_type"],
                title_old=r["title_old"],
                content_preview=_content_preview(r["content_old"]),
                parent_id_old=r["parent_id_old"],
            )
            for r in rows
        ]
        return HistoryListResponse(versions=versions)


@router.get("/{item_id}/activities", response_model=ActivityListResponse)
def list_item_activities(
    item_id: int,
    limit: int = Query(100, ge=1, le=1000, description="Most activities to return"),
):
    """An item's activities (watches, searches, prompts), newest first."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        total = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT a.id, a.verb, a.occurred_at, a.source, a.context_id, a.details,
                   a.actor_id, p.name AS actor
               FROM activities a LEFT JOIN people p ON p.id = a.actor_id
               WHERE a.item_id = ? ORDER BY a.occurred_at DESC, a.id DESC LIMIT ?""",
            (item_id, limit),
        ).fetchall()
        return ActivityListResponse(
            activities=[
                Activity(
                    id=r["id"],
                    verb=r["verb"],
                    occurred_at=r["occurred_at"],
                    source=r["source"],
                    context_id=r["context_id"],
                    details=_json_object(r["details"]),
                    actor_id=r["actor_id"],
                    actor=r["actor"],
                )
                for r in rows
            ],
            total=total,
        )


@router.get("/{item_id}/links", response_model=ItemLinkListResponse)
def list_item_links(item_id: int):
    """An item's confirmed links to and from other items, such as a post it links."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return ItemLinkListResponse(links=[ItemLink(**link) for link in links_of(conn, item_id)])


@router.get("/{item_id}/agents", response_model=ItemAgentListResponse)
def list_item_agents(item_id: int):
    """Who made, voiced, published or received an item, with each one's accounts."""
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return ItemAgentListResponse(agents=[ItemAgent(**a) for a in agents_for(conn, item_id)])


@router.delete("/{item_id}/links")
def remove_item_link(
    item_id: int,
    other_id: int = Query(..., description="The item at the link's other end"),
    kind: str = Query(..., description="The link's kind, such as references"),
    direction: Literal["outgoing", "incoming"] = Query("outgoing"),
):
    """Remove a link: it is marked rejected, so the linker never adds it again."""
    from_id, to_id = (item_id, other_id) if direction == "outgoing" else (other_id, item_id)
    with get_db() as conn:
        if not reject_link(conn, from_id, to_id, kind):
            raise HTTPException(status_code=404, detail="No such link")
        conn.commit()
    return {"status": "rejected"}


@router.get("/{item_id}/history/{version_id}", response_model=HistoryVersionDetail)
def get_item_history_version(item_id: int, version_id: int):
    """Get the full snapshot for one history version."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM item_history WHERE id = ? AND item_id = ?",
            (version_id, item_id),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_id} not found for item {item_id}",
            )
        return HistoryVersionDetail(
            id=row["id"],
            item_id=row["item_id"],
            changed_at=row["changed_at"],
            change_reason=row["change_reason"],
            change_type=row["change_type"],
            title_old=row["title_old"],
            content_old=row["content_old"],
            author_old=row["author_old"],
            url_old=row["url_old"],
            metadata_old=_json_object(row["metadata_old"]),
            is_own_content_old=bool(row["is_own_content_old"])
            if row["is_own_content_old"] is not None
            else None,
            parent_id_old=row["parent_id_old"],
        )


@router.post("/{item_id}/history/{version_id}/restore", response_model=ItemResponse)
def restore_item_history_version(
    item_id: int,
    version_id: int,
    restore_parent: bool = Query(
        True,
        description=(
            "If False, leave the item's current parent_id alone instead of "
            "writing the snapshot's parent_id_old. Use this for history rows "
            "captured before Migration 9, where parent_id_old is always NULL "
            "but the item may currently have a real parent."
        ),
    ),
):
    """Restore an item to a previous version.

    Writes the snapshot fields back to items; the resulting UPDATE captures
    the pre-restore state as a new history row tagged 'restore'. NULL values
    in the snapshot are written through verbatim — note that history rows
    captured before Migration 9 always have parent_id_old=NULL, so callers
    should pass `restore_parent=false` for those rows to avoid silently
    detaching a still-parented item.
    """
    with get_db() as conn:
        snapshot = conn.execute(
            "SELECT * FROM item_history WHERE id = ? AND item_id = ?",
            (version_id, item_id),
        ).fetchone()
        if not snapshot:
            raise HTTPException(
                status_code=404,
                detail=f"Version {version_id} not found for item {item_id}",
            )

        # content has NOT NULL on items, so refuse if snapshot's content_old is NULL
        # (would be a pathological pre-trigger row).
        if snapshot["content_old"] is None:
            raise HTTPException(
                status_code=400,
                detail="Snapshot is missing content; cannot restore",
            )

        pre_max = max_history_id(conn)
        if restore_parent:
            conn.execute(
                """UPDATE items SET
                       title = ?,
                       content = ?,
                       author = ?,
                       url = ?,
                       metadata = ?,
                       is_own_content = ?,
                       parent_id = ?
                   WHERE id = ?""",
                (
                    snapshot["title_old"],
                    snapshot["content_old"],
                    snapshot["author_old"],
                    snapshot["url_old"],
                    snapshot["metadata_old"],
                    snapshot["is_own_content_old"],
                    snapshot["parent_id_old"],
                    item_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE items SET
                       title = ?,
                       content = ?,
                       author = ?,
                       url = ?,
                       metadata = ?,
                       is_own_content = ?
                   WHERE id = ?""",
                (
                    snapshot["title_old"],
                    snapshot["content_old"],
                    snapshot["author_old"],
                    snapshot["url_old"],
                    snapshot["metadata_old"],
                    snapshot["is_own_content_old"],
                    item_id,
                ),
            )
        mark_recent_history(conn, pre_max, "restore", item_ids=[item_id])
        conn.commit()

        re_embed_item(conn, item_id)

        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _enrich_item(conn, Item.from_row(row))


@router.get("/{item_id}", response_model=ItemResponse)
def get_item(item_id: int):
    """Get a single item by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        return _enrich_item(conn, Item.from_row(row))


@router.post("/{item_id}/tags", response_model=ItemResponse, status_code=201)
def add_item_tag(item_id: int, body: TagAddRequest):
    """Add a tag to an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        add_tag(conn, item_id, body.name)
        return _enrich_item(conn, Item.from_row(row))


@router.delete("/{item_id}/tags/{tag_name}", response_model=ItemResponse)
def remove_item_tag(item_id: int, tag_name: str):
    """Remove a tag from an item."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        remove_tag(conn, item_id, tag_name)
        return _enrich_item(conn, Item.from_row(row))

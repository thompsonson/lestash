"""Items as the API returns them: enriched with names, previews, kind and counts.

One builder for every route that returns items (items, collections, similar items).
"""

import html
import re

from lestash.core.database import get_item_media, get_tags
from lestash.core.enrichment import get_author_actor, get_byline, get_item_subtype, get_preview
from lestash.core.kinds import KINDS, item_kind, kind_detail
from lestash.models.item import Item

from lestash_server.models import ItemResponse, MediaResponse

PREVIEW_CHARS = 120
# LinkedIn's preview can end with its comment count: "Context is everywhere! | 13 comments…"
_COMMENT_COUNT = re.compile(r"\s*\|\s*[\d,]+ comments? on LinkedIn\s*$")


def preview_title(text: str | None, stub_of: str | None) -> str | None:
    """A stub's title from its post's cached preview: the first line, cut short."""
    # The preview is the page's og:description as written: HTML entities and all
    text = html.unescape(text or "")
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = _COMMENT_COUNT.sub("", line)
    if not line:
        return None
    if len(line) > PREVIEW_CHARS:
        line = line[: PREVIEW_CHARS - 1].rstrip() + "…"
    return f"A reply on: {line}" if stub_of == "comment" else line


def stub_post(conn, item: Item) -> tuple[str | None, str | None]:
    """A LinkedIn stub's title and author, from its post's cached public preview
    (lestash_linkedin.feed_preview). (None, None) when none is cached (#322)."""
    meta = item.metadata or {}
    urn = meta.get("post_urn")
    if not isinstance(urn, str):
        return None, None
    row = conn.execute(
        "SELECT content_preview, author_name FROM post_cache WHERE urn = ?", (urn,)
    ).fetchone()
    if not row:
        return None, None
    title = preview_title(row[0], "comment" if meta.get("comment_urn") else "post")
    return title, html.unescape(row[1]) if row[1] else None


def media_responses(conn, item_id: int) -> list[MediaResponse]:
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


def enrich_item(conn, item: Item) -> ItemResponse:
    """Convert an Item to an enriched API response."""
    author_display, actor_display = get_author_actor(conn, item)
    child_count, reaction_count = conn.execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE kind = 'reaction') FROM items WHERE parent_id = ?",
        (item.id,),
    ).fetchone()
    kind = item.kind or item_kind(item.source_type, item.metadata, item.parent_id, item.held)
    parent = None
    if item.parent_id is not None:
        parent = conn.execute(
            "SELECT title, kind FROM items WHERE id = ?", (item.parent_id,)
        ).fetchone()
    byline = get_byline(conn, item, kind)
    title = item.title
    copies = 0
    if not item.held:
        # Known only by its address: named by its post's preview, and the copies of it
        # you keep under it (#322)
        if not (title or "").strip():
            title, post_author = stub_post(conn, item)
            if post_author and author_display in (None, "", "-"):
                author_display = post_author
        copies = conn.execute(
            "SELECT COUNT(*) FROM items WHERE parent_id = ? AND json_valid(metadata)"
            " AND json_extract(metadata, '$.copy_of') IS NOT NULL",
            (item.id,),
        ).fetchone()[0]
    copy = isinstance(item.metadata, dict) and item.metadata.get("copy_of") is not None
    return ItemResponse(
        id=item.id,
        source_type=item.source_type,
        source_id=item.source_id,
        url=item.url,
        title=title,
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
        reaction_count=reaction_count,
        kind=kind,
        kind_label="Copy" if copy else (KINDS[kind].label if kind in KINDS else kind),
        copies=copies,
        kind_detail=kind_detail(kind, item.metadata),
        parent_title=parent[0] if parent else None,
        parent_kind=parent[1] if parent else None,
        by_id=byline.person_id if byline else None,
        by_name=byline.name if byline else None,
        by_self=byline.is_self if byline else False,
        media=media_responses(conn, item.id),
    )

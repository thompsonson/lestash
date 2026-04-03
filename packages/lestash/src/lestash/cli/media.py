"""Media management CLI commands."""

import json

import typer
from rich.console import Console

from lestash.core.database import add_item_media, get_connection

app = typer.Typer(help="Media attachment management.")
console = Console()


# Source-specific extraction rules: (metadata_key, media_type, is_list)
_BACKFILL_RULES: dict[str, list[tuple[str, str, bool]]] = {
    "microblog": [("photos", "image", True)],
    "youtube": [("thumbnail_url", "thumbnail", False)],
    "arxiv": [("pdf_url", "pdf", False)],
    "audible": [("cover_url", "thumbnail", False)],
    "linkedin": [("media_category", "_linkedin_special", False)],
}


def _backfill_linkedin(metadata: dict, item_id: int, conn) -> int:
    """Extract media from LinkedIn metadata.raw activity data."""
    count = 0
    category = metadata.get("media_category")
    raw = metadata.get("raw", {})
    activity = raw.get("activity", {})
    share_content = activity.get("specificContent", {}).get("com.linkedin.ugc.ShareContent", {})
    raw_media = share_content.get("media", [])

    for i, m in enumerate(raw_media):
        if category == "IMAGE":
            asset_urn = m.get("media", "")
            if asset_urn:
                add_item_media(
                    conn,
                    item_id,
                    media_type="image",
                    url=asset_urn,
                    alt_text=m.get("title", {}).get("text"),
                    position=i,
                    source_origin="backfill",
                    _commit=False,
                )
                count += 1
        elif category == "ARTICLE":
            original_url = m.get("originalUrl", "")
            if original_url:
                add_item_media(
                    conn,
                    item_id,
                    media_type="link",
                    url=original_url,
                    alt_text=m.get("title", {}).get("text"),
                    position=i,
                    source_origin="backfill",
                    _commit=False,
                )
                count += 1

    # Also handle article_url in top-level metadata (from posting)
    article_url = metadata.get("article_url")
    if article_url:
        add_item_media(
            conn,
            item_id,
            media_type="link",
            url=article_url,
            alt_text=metadata.get("article_title"),
            source_origin="backfill",
            _commit=False,
        )
        count += 1

    return count


def _backfill_bluesky(conn, dry_run: bool) -> int:
    """Re-fetch Bluesky image posts to extract blob CIDs for CDN URLs."""
    rows = conn.execute(
        """SELECT id, source_id, metadata FROM items
           WHERE source_type = 'bluesky'
             AND json_extract(metadata, '$.images') IS NOT NULL
             AND id NOT IN (SELECT item_id FROM item_media)"""
    ).fetchall()

    if not rows:
        return 0

    if dry_run:
        return len(rows)

    try:
        from lestash_bluesky.client import create_client
    except ImportError:
        console.print("[dim]lestash-bluesky not installed, skipping[/dim]")
        return 0

    try:
        client = create_client()
    except Exception as e:
        console.print(f"[dim]Bluesky auth failed ({e}), skipping[/dim]")
        return 0

    count = 0
    # Process in batches of 25 (API limit)
    uris = [(row["id"], row["source_id"], json.loads(row["metadata"])) for row in rows]
    for batch_start in range(0, len(uris), 25):
        batch = uris[batch_start : batch_start + 25]
        batch_uris = [uri for _, uri, _ in batch]
        try:
            resp = client.get_posts(batch_uris)
        except Exception:
            continue

        # Map URI -> post for lookup
        post_map = {p.uri: p for p in resp.posts}

        for item_id, uri, metadata in batch:
            post = post_map.get(uri)
            if not post or not post.record or not hasattr(post.record, "embed"):
                continue
            embed = post.record.embed
            if not embed or not hasattr(embed, "images"):
                continue

            author_did = metadata.get("author_did", "")
            for i, img in enumerate(embed.images):
                blob = getattr(img, "image", None)
                if blob and getattr(blob, "ref", None):
                    ref = blob.ref
                    cid = ref.link if hasattr(ref, "link") else str(ref)
                    cdn_url = (
                        f"https://cdn.bsky.app/img/feed_thumbnail/plain/{author_did}/{cid}@jpeg"
                    )
                    add_item_media(
                        conn,
                        item_id,
                        media_type="image",
                        url=cdn_url,
                        alt_text=img.alt,
                        mime_type=getattr(blob, "mime_type", None),
                        position=i,
                        source_origin="backfill",
                        _commit=False,
                    )
                    count += 1

    conn.commit()
    return count


@app.command()
def backfill(
    source: str | None = typer.Option(None, help="Only backfill this source type"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be created"),
) -> None:
    """Populate item_media from existing item metadata."""
    with get_connection() as conn:
        # Bluesky needs special handling (re-fetches from API)
        if source is None or source == "bluesky":
            bs_count = _backfill_bluesky(conn, dry_run)
            if bs_count:
                prefix = "[dim](dry run)[/dim] " if dry_run else ""
                console.print(f"{prefix}bluesky: {bs_count} media entries (via API)")

        query = "SELECT id, source_type, metadata FROM items WHERE metadata IS NOT NULL"
        params: list = []
        if source:
            query += " AND source_type = ?"
            params.append(source)

        rows = conn.execute(query, params).fetchall()
        totals: dict[str, int] = {}

        for row in rows:
            item_id = row["id"]
            source_type = row["source_type"]
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                continue

            rules = _BACKFILL_RULES.get(source_type, [])
            for key, media_type, is_list in rules:
                if media_type == "_linkedin_special":
                    count = _backfill_linkedin(metadata, item_id, conn)
                    totals[source_type] = totals.get(source_type, 0) + count
                    continue

                value = metadata.get(key)
                if not value:
                    continue

                urls = value if is_list else [value]
                for i, url in enumerate(urls):
                    if not dry_run:
                        add_item_media(
                            conn,
                            item_id,
                            media_type=media_type,
                            url=url,
                            position=i,
                            source_origin="backfill",
                            _commit=False,
                        )
                    totals[source_type] = totals.get(source_type, 0) + 1

        if not dry_run:
            conn.commit()

        prefix = "[dim](dry run)[/dim] " if dry_run else ""
        if totals:
            for src, count in sorted(totals.items()):
                console.print(f"{prefix}{src}: {count} media entries")
            console.print(f"\n{prefix}[green]Total: {sum(totals.values())} media entries[/green]")
        else:
            console.print("No media found to backfill.")

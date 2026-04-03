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


@app.command()
def backfill(
    source: str | None = typer.Option(None, help="Only backfill this source type"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be created"),
) -> None:
    """Populate item_media from existing item metadata."""
    with get_connection() as conn:
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

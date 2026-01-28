"""Item commands for Le Stash CLI."""

import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lestash.core.config import Config
from lestash.core.database import get_connection, get_person_profile, get_post_cache
from lestash.models.item import Item


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")[:50]


def _format_microblog_draft(item: Item) -> str:
    """Format an item as a Micro.blog draft markdown file.

    Args:
        item: The item to format

    Returns:
        Markdown string with YAML frontmatter
    """
    # Generate title
    if item.title:
        title = f"Notes on: {item.title}"
    else:
        preview = item.content[:50].replace("\n", " ")
        title = f"Notes on: {preview}..."

    # Build frontmatter
    frontmatter = f"""---
title: "{title}"
status: "draft"
type: "post"
location: "drafts"
---"""

    # Build content with source reference
    lines = [frontmatter, ""]

    # Add source metadata as HTML comment
    lines.append(f"<!-- Source: {item.source_type} item #{item.id} -->")
    if item.url:
        lines.append(f"<!-- Original: {item.url} -->")
    lines.append("")

    # Add placeholder for user's notes
    lines.append("[Your notes here]")
    lines.append("")

    # Add reference section
    lines.append("---")
    lines.append("")

    if item.title and item.url:
        lines.append(f"*Reference: [{item.title}]({item.url})*")
    elif item.title:
        lines.append(f"*Reference: {item.title}*")
    elif item.url:
        lines.append(f"*Reference: {item.url}*")
    else:
        lines.append(f"*Source: {item.source_type} (lestash item #{item.id})*")

    # Add creation date if available
    if item.created_at:
        lines.append(f"*Date: {item.created_at.strftime('%Y-%m-%d')}*")

    return "\n".join(lines)


def _resolve_author(conn, author: str | None) -> str:
    """Resolve author URN to display name if available."""
    if not author:
        return "-"
    # Try to look up profile
    profile = get_person_profile(conn, author)
    if profile and profile.get("display_name"):
        return profile["display_name"]
    # Return full URN so user can copy it to add profiles
    return author


def _get_author_actor(conn, item: Item) -> tuple[str, str]:
    """Extract author and actor for display.

    For reactions:
    - Author = who wrote the content being reacted to
    - Actor = who made the reaction

    Args:
        conn: Database connection
        item: The item to extract author/actor from

    Returns:
        Tuple of (author_display, actor_display)
    """
    raw = item.metadata.get("raw", {}) if item.metadata else {}
    owner_urn = raw.get("owner")
    actor_urn = raw.get("actor")

    # Resolve actor URN
    actor_display = _resolve_author(conn, actor_urn) if actor_urn else "-"

    # Determine author based on scenario
    if owner_urn and actor_urn and owner_urn != actor_urn:
        # Someone else reacted to YOUR content
        author_display = "You"
    else:
        # You reacted to someone else's content
        # Try to get author from post_cache enrichment
        target_urn = (
            item.metadata.get("reacted_to") or item.metadata.get("commented_on")
            if item.metadata
            else None
        )
        if target_urn:
            cached = get_post_cache(conn, target_urn)
            author_display = cached["author_name"] if cached and cached.get("author_name") else "-"
        else:
            # Not a reaction/comment - fall back to item.author
            author_display = _resolve_author(conn, item.author)

    return author_display, actor_display


def _get_item_subtype(item: Item) -> str:
    """Derive a descriptive type from source_type and metadata.

    Returns format: source/subtype, e.g.:
    - linkedin/post
    - linkedin/comment
    - linkedin/reaction/like (→post)
    - linkedin/invitation
    - bluesky (no subtype available)
    """
    source = item.source_type

    if not item.metadata:
        return source

    resource = item.metadata.get("resource_name", "")
    reaction_type = item.metadata.get("reaction_type", "")
    reacted_to = item.metadata.get("reacted_to", "")

    subtype = None
    if resource == "ugcPosts":
        subtype = "post"
    elif resource == "socialActions/comments":
        subtype = "comment"
    elif resource == "socialActions/likes":
        reaction_label = reaction_type.lower() if reaction_type else "like"
        if reacted_to:
            target = "comment" if "comment:" in reacted_to else "post"
            subtype = f"reaction/{reaction_label} (→{target})"
        else:
            subtype = f"reaction/{reaction_label}"
    elif resource == "invitations":
        subtype = "invitation"
    elif resource == "messages":
        subtype = "message"
    elif resource:
        # Unknown resource, use as-is
        subtype = resource.replace("socialActions/", "")

    if subtype:
        return f"{source}/{subtype}"
    return source


def _get_preview(conn, item: Item, max_length: int = 50) -> str:
    """Get preview text for display.

    For reactions (LIKE, CELEBRATE, etc.) and comments, if we have cached
    content for the target, show that content instead of the activity URN.

    Args:
        conn: Database connection
        item: The item to generate preview for
        max_length: Maximum preview length

    Returns:
        Preview text string
    """
    # Check if this is a reaction or comment with a target
    if item.metadata:
        target_urn = item.metadata.get("reacted_to") or item.metadata.get("commented_on")
        if target_urn:
            # Detect if target is a comment or post
            is_comment_target = target_urn.startswith("urn:li:comment:")

            # Look up cached content
            cached = get_post_cache(conn, target_urn)
            if cached and cached.get("content_preview"):
                # Extract emoji and reaction type from original content
                # Format: "👍 LIKE on activity:123" -> "👍 LIKE"
                original = item.content
                parts = original.split(" on ")
                prefix = parts[0] if parts else ""

                # Add "(comment)" indicator for comment reactions
                if is_comment_target:
                    prefix = f"{prefix} (comment)"

                # Check if this is a reaction from someone else (has reactor_name)
                if cached.get("reactor_name"):
                    # Format: "👍 LIKE from Mike: 'Your post...'"
                    prefix = f"{prefix} from {cached['reactor_name']}"

                # Build new preview with cached content
                cached_preview = cached["content_preview"][:max_length]
                if len(cached["content_preview"]) > max_length:
                    cached_preview += "..."

                return f'{prefix}: "{cached_preview}"'

            # Not enriched but is a comment reaction - add indicator to original content
            if is_comment_target and " on " in item.content:
                # Format: "👍 LIKE on (activity:xxx" -> "👍 LIKE (comment) on (activity:xxx"
                parts = item.content.split(" on ", 1)
                if len(parts) == 2:
                    preview = f"{parts[0]} (comment) on {parts[1]}"
                    if len(preview) > max_length:
                        return preview[:max_length] + "..."
                    return preview

    # Fall back to standard preview
    if item.title:
        return item.title
    elif len(item.content) > max_length:
        return item.content[:max_length] + "..."
    else:
        return item.content


app = typer.Typer(help="Manage items in your knowledge base.")
console = Console()


@app.command("list")
def list_items(
    source: Annotated[
        str | None, typer.Option("--source", "-s", help="Filter by source type")
    ] = None,
    own: Annotated[bool | None, typer.Option("--own", help="Show only your own content")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Limit results")] = 20,
) -> None:
    """List items in the knowledge base."""
    config = Config.load()

    with get_connection(config) as conn:
        query = "SELECT * FROM items WHERE 1=1"
        params: list = []

        if source:
            query += " AND source_type = ?"
            params.append(source)

        if own is not None:
            query += " AND is_own_content = ?"
            params.append(own)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            console.print("[dim]No items found.[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", style="dim")
        table.add_column("Type")
        table.add_column("Title / Content Preview")
        table.add_column("Author")
        table.add_column("Actor")
        table.add_column("Created")

        for row in rows:
            item = Item.from_row(row)
            preview = _get_preview(conn, item)
            author, actor = _get_author_actor(conn, item)
            created = item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "-"
            subtype = _get_item_subtype(item)
            table.add_row(
                str(item.id),
                subtype,
                preview,
                author,
                actor,
                created,
            )

        console.print(table)


@app.command("search")
def search_items(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Limit results")] = 20,
) -> None:
    """Search items using full-text search."""
    config = Config.load()

    with get_connection(config) as conn:
        cursor = conn.execute(
            """
            SELECT items.* FROM items
            JOIN items_fts ON items.id = items_fts.rowid
            WHERE items_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        )
        rows = cursor.fetchall()

        if not rows:
            console.print(f"[dim]No items found matching '{query}'.[/dim]")
            return

        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", style="dim")
        table.add_column("Type")
        table.add_column("Title / Content Preview")
        table.add_column("Author")
        table.add_column("Actor")
        table.add_column("Created")

        for row in rows:
            item = Item.from_row(row)
            preview = _get_preview(conn, item)
            author, actor = _get_author_actor(conn, item)
            created = item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "-"
            subtype = _get_item_subtype(item)
            table.add_row(
                str(item.id),
                subtype,
                preview,
                author,
                actor,
                created,
            )

        console.print(table)


@app.command("show")
def show_item(
    item_id: Annotated[int, typer.Argument(help="Item ID to show")],
) -> None:
    """Show details of a specific item."""
    config = Config.load()

    with get_connection(config) as conn:
        cursor = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = cursor.fetchone()

        if not row:
            console.print(f"[red]Item {item_id} not found.[/red]")
            raise typer.Exit(1)

        item = Item.from_row(row)

        # Resolve author profile
        author_display = _resolve_author(conn, item.author)
        author_profile = get_person_profile(conn, item.author) if item.author else None

        console.print(f"[bold]ID:[/bold] {item.id}")
        console.print(f"[bold]Source:[/bold] {item.source_type}")
        if item.source_id:
            console.print(f"[bold]Source ID:[/bold] {item.source_id}")
        if item.url:
            console.print(f"[bold]URL:[/bold] {item.url}")
        if item.title:
            console.print(f"[bold]Title:[/bold] {item.title}")
        console.print(f"[bold]Author:[/bold] {author_display}")
        if author_profile and author_profile.get("profile_url"):
            console.print(f"[bold]Author Profile:[/bold] {author_profile['profile_url']}")

        # Show what this item is responding to (reaction or comment target)
        if item.metadata:
            target_urn = item.metadata.get("reacted_to") or item.metadata.get("commented_on")
            if target_urn:
                # Detect if target is a comment or post
                is_comment_target = target_urn.startswith("urn:li:comment:")
                if "reacted_to" in item.metadata:
                    target_label = "Reacted To Comment" if is_comment_target else "Reacted To"
                else:
                    target_label = "Commented On"
                console.print(f"[bold]{target_label}:[/bold] {target_urn}")

                # Generate URL
                target_url = None
                if target_urn.startswith("urn:li:activity:"):
                    target_url = f"https://www.linkedin.com/feed/update/{target_urn}"
                elif is_comment_target:
                    # Extract parent activity from comment URN
                    match = re.search(r"activity:(\d+)", target_urn)
                    if match:
                        parent_activity = match.group(1)
                        target_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{parent_activity}"
                if target_url:
                    console.print(f"[bold]{target_label} URL:[/bold] {target_url}")

                # Show cached content if available
                cached = get_post_cache(conn, target_urn)
                if cached:
                    content_label = "Comment Content" if is_comment_target else "Post Content"
                    if cached.get("content_preview"):
                        console.print()
                        console.print(f"[bold]{content_label}:[/bold]")
                        preview = cached["content_preview"]
                        if len(preview) > 300:
                            console.print(f"[dim]{preview[:300]}...[/dim]")
                        else:
                            console.print(f"[dim]{preview}[/dim]")
                        if cached.get("author_name"):
                            console.print(f"[dim]— {cached['author_name']}[/dim]")
                    if cached.get("reactor_name"):
                        console.print(
                            f"[bold]Reacted By:[/bold] {cached['reactor_name']} (your content)"
                        )
                    if cached.get("image_path"):
                        console.print(f"[bold]Screenshot:[/bold] {cached['image_path']}")

        if item.created_at:
            console.print(f"[bold]Created:[/bold] {item.created_at}")
        console.print(f"[bold]Fetched:[/bold] {item.fetched_at}")
        console.print(f"[bold]Own Content:[/bold] {item.is_own_content}")
        console.print()
        console.print("[bold]Content:[/bold]")
        console.print(item.content)

        if item.metadata:
            console.print()
            console.print("[bold]Metadata:[/bold]")
            console.print(json.dumps(item.metadata, indent=2))


@app.command("export")
def export_items(
    output: Annotated[
        str, typer.Option("--output", "-o", help="Output file path")
    ] = "lestash-export.json",
    source: Annotated[
        str | None, typer.Option("--source", "-s", help="Filter by source type")
    ] = None,
) -> None:
    """Export items to JSON."""
    config = Config.load()

    with get_connection(config) as conn:
        query = "SELECT * FROM items"
        params: list = []

        if source:
            query += " WHERE source_type = ?"
            params.append(source)

        query += " ORDER BY created_at DESC"

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

    items = [Item.from_row(row).model_dump(mode="json") for row in rows]

    with open(output, "w") as f:
        json.dump(items, f, indent=2, default=str)

    console.print(f"[green]Exported {len(items)} items to {output}[/green]")


@app.command("draft")
def create_draft(
    item_id: Annotated[int, typer.Argument(help="Item ID to create draft from")],
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output directory (default: current dir)"),
    ] = None,
    filename: Annotated[
        str | None,
        typer.Option("--filename", "-f", help="Output filename (auto-generated if not provided)"),
    ] = None,
) -> None:
    """Create a Micro.blog draft from an item.

    Generates a markdown file with YAML frontmatter compatible with
    the vscode.micro.blog extension's drafts folder structure.

    Example:
        lestash items draft 248 --output ~/blog/content/drafts/
    """
    config = Config.load()

    with get_connection(config) as conn:
        cursor = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = cursor.fetchone()

        if not row:
            console.print(f"[red]Item {item_id} not found.[/red]")
            raise typer.Exit(1)

        item = Item.from_row(row)

    # Generate markdown content
    content = _format_microblog_draft(item)

    # Determine output path
    if filename:
        name = filename if filename.endswith(".md") else f"{filename}.md"
    else:
        # Auto-generate filename from title or content
        base = item.title or item.content[:30]
        slug = _slugify(base)
        name = f"draft-{item.source_type}-{slug}.md"

    if output:
        output_path = Path(output).expanduser() / name
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path(name)

    # Write the file
    output_path.write_text(content)

    console.print(f"[green]Created draft: {output_path}[/green]")
    console.print(f"[dim]Source: {item.source_type} item #{item.id}[/dim]")
    if item.url:
        console.print(f"[dim]Reference: {item.url}[/dim]")

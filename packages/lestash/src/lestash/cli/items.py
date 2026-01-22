"""Item commands for Le Stash CLI."""

import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lestash.core.config import Config
from lestash.core.database import get_connection, get_person_profile
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
        table.add_column("Created")

        for row in rows:
            item = Item.from_row(row)
            preview = (
                item.title or item.content[:50] + "..." if len(item.content) > 50 else item.content
            )
            created = item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "-"
            author_display = _resolve_author(conn, item.author)
            table.add_row(
                str(item.id),
                item.source_type,
                preview,
                author_display,
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
        table.add_column("Created")

        for row in rows:
            item = Item.from_row(row)
            preview = (
                item.title or item.content[:50] + "..." if len(item.content) > 50 else item.content
            )
            created = item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "-"
            author_display = _resolve_author(conn, item.author)
            table.add_row(
                str(item.id),
                item.source_type,
                preview,
                author_display,
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
            target_type = "Reacted To" if "reacted_to" in item.metadata else "Commented On"
            console.print(f"[bold]{target_type}:[/bold] {target_urn}")
            # Generate URL if it's an activity URN
            if target_urn.startswith("urn:li:activity:"):
                target_url = f"https://www.linkedin.com/feed/update/{target_urn}"
                console.print(f"[bold]{target_type} URL:[/bold] {target_url}")

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

"""Item commands for Le Stash CLI."""

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.models.item import Item

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
        table.add_row(
            str(item.id),
            item.source_type,
            preview,
            item.author or "-",
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
        table.add_row(
            str(item.id),
            item.source_type,
            preview,
            item.author or "-",
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

    console.print(f"[bold]ID:[/bold] {item.id}")
    console.print(f"[bold]Source:[/bold] {item.source_type}")
    if item.source_id:
        console.print(f"[bold]Source ID:[/bold] {item.source_id}")
    if item.url:
        console.print(f"[bold]URL:[/bold] {item.url}")
    if item.title:
        console.print(f"[bold]Title:[/bold] {item.title}")
    console.print(f"[bold]Author:[/bold] {item.author or '-'}")
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

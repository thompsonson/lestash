"""Source commands for Le Stash CLI."""

import json
from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.plugins.loader import load_plugins

app = typer.Typer(help="Manage content sources.")
console = Console()


@app.command("list")
def list_sources() -> None:
    """List installed source plugins."""
    plugins = load_plugins()

    if not plugins:
        console.print("[dim]No source plugins installed.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Status")

    config = Config.load()
    with get_connection(config) as conn:
        for name, plugin in plugins.items():
            cursor = conn.execute(
                "SELECT enabled, last_sync FROM sources WHERE source_type = ?",
                (name,),
            )
            row = cursor.fetchone()

            if row:
                status = "enabled" if row["enabled"] else "disabled"
                if row["last_sync"]:
                    status += f" (last sync: {row['last_sync']})"
            else:
                status = "not configured"

            table.add_row(name, plugin.description, status)

    console.print(table)


@app.command("sync")
def sync_source(
    source_name: Annotated[str | None, typer.Argument(help="Source to sync (omit for all)")] = None,
    all_sources: Annotated[bool, typer.Option("--all", help="Sync all enabled sources")] = False,
) -> None:
    """Sync items from a source."""
    plugins = load_plugins()

    if not plugins:
        console.print("[red]No source plugins installed.[/red]")
        raise typer.Exit(1)

    if source_name:
        sources_to_sync = [source_name]
    elif all_sources:
        sources_to_sync = list(plugins.keys())
    else:
        console.print("[red]Specify a source name or use --all[/red]")
        raise typer.Exit(1)

    config = Config.load()

    for name in sources_to_sync:
        if name not in plugins:
            console.print(f"[red]Unknown source: {name}[/red]")
            continue

        plugin = plugins[name]
        plugin_config = config.get_plugin_config(name)

        console.print(f"[bold]Syncing {name}...[/bold]")

        with get_connection(config) as conn:
            # Log sync start
            started_at = datetime.now()
            cursor = conn.execute(
                "INSERT INTO sync_log (source_type, started_at, status) VALUES (?, ?, ?)",
                (name, started_at, "running"),
            )
            sync_id = cursor.lastrowid
            conn.commit()

            items_added = 0
            items_updated = 0
            error_message = None

            try:
                for item in plugin.sync(plugin_config):
                    # Try to insert, update on conflict
                    metadata_json = json.dumps(item.metadata) if item.metadata else None
                    cursor = conn.execute(
                        """
                        INSERT INTO items (
                            source_type, source_id, url, title, content,
                            author, created_at, is_own_content, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_type, source_id) DO UPDATE SET
                            url = excluded.url,
                            title = excluded.title,
                            content = excluded.content,
                            author = excluded.author,
                            is_own_content = excluded.is_own_content,
                            metadata = excluded.metadata
                        """,
                        (
                            item.source_type,
                            item.source_id,
                            item.url,
                            item.title,
                            item.content,
                            item.author,
                            item.created_at,
                            item.is_own_content,
                            metadata_json,
                        ),
                    )
                    if cursor.rowcount > 0:
                        items_added += 1

                conn.commit()

                # Update source last_sync
                conn.execute(
                    """
                    INSERT INTO sources (source_type, last_sync)
                    VALUES (?, ?)
                    ON CONFLICT(source_type) DO UPDATE SET last_sync = excluded.last_sync
                    """,
                    (name, datetime.now()),
                )
                conn.commit()

                status = "completed"
            except Exception as e:
                status = "failed"
                error_message = str(e)
                console.print(f"[red]Error syncing {name}: {e}[/red]")

            # Update sync log
            conn.execute(
                """
                UPDATE sync_log SET
                    completed_at = ?,
                    status = ?,
                    items_added = ?,
                    items_updated = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (datetime.now(), status, items_added, items_updated, error_message, sync_id),
            )
            conn.commit()

        if status == "completed":
            console.print(f"[green]Synced {name}: {items_added} items added[/green]")


@app.command("status")
def source_status() -> None:
    """Show sync status for all sources."""
    config = Config.load()

    with get_connection(config) as conn:
        cursor = conn.execute(
            """
            SELECT source_type, started_at, completed_at, status,
                   items_added, items_updated, error_message
            FROM sync_log
            ORDER BY started_at DESC
            LIMIT 20
            """
        )
        rows = cursor.fetchall()

    if not rows:
        console.print("[dim]No sync history.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Source")
    table.add_column("Started")
    table.add_column("Status")
    table.add_column("Items")
    table.add_column("Error")

    for row in rows:
        started = row["started_at"][:16] if row["started_at"] else "-"
        items = f"+{row['items_added']}" if row["items_added"] else "-"
        error = (
            row["error_message"][:30] + "..."
            if row["error_message"] and len(row["error_message"]) > 30
            else (row["error_message"] or "-")
        )

        status_style = {
            "completed": "green",
            "failed": "red",
            "running": "yellow",
        }.get(row["status"], "")

        table.add_row(
            row["source_type"],
            started,
            f"[{status_style}]{row['status']}[/{status_style}]" if status_style else row["status"],
            items,
            error,
        )

    console.print(table)

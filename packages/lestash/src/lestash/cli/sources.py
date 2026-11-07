"""Source commands for Le Stash CLI."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.core.sync import SyncAlreadyRunning, SyncRun, SyncStore, sigterm_as_exit
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


def _disabled_sources(conn) -> set[str]:
    """Source types explicitly marked disabled in the sources table."""
    return {
        row[0]
        for row in conn.execute("SELECT source_type FROM sources WHERE enabled = 0").fetchall()
    }


def _set_source_enabled(source_name: str, enabled: bool, config: Config | None = None) -> None:
    """Upsert the enabled flag for a source (creates the row if absent)."""
    config = config or Config.load()
    with get_connection(config) as conn:
        conn.execute(
            """
            INSERT INTO sources (source_type, enabled) VALUES (?, ?)
            ON CONFLICT(source_type) DO UPDATE SET enabled = excluded.enabled
            """,
            (source_name, 1 if enabled else 0),
        )
        conn.commit()


@app.command("disable")
def disable_source(
    source_name: Annotated[str, typer.Argument(help="Source to stop syncing")],
) -> None:
    """Stop a source from being synced by `sync --all` (existing data is kept)."""
    _set_source_enabled(source_name, False)
    console.print(f"[yellow]Disabled {source_name}[/yellow] — `sync --all` will skip it.")


@app.command("enable")
def enable_source(
    source_name: Annotated[str, typer.Argument(help="Source to resume syncing")],
) -> None:
    """Re-enable a previously disabled source."""
    _set_source_enabled(source_name, True)
    console.print(f"[green]Enabled {source_name}[/green]")


def _say_waiting(name: str) -> Callable[[], None]:
    return lambda: console.print(f"  [dim]Waiting for the running {name} sync to finish[/dim]")


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

    config = Config.load()

    if source_name:
        # An explicit source name syncs even if disabled.
        sources_to_sync = [source_name]
    elif all_sources:
        # --all syncs every registered plugin except those explicitly disabled.
        with get_connection(config) as conn:
            disabled = _disabled_sources(conn)
        sources_to_sync = [name for name in plugins if name not in disabled]
        for name in sorted(disabled & set(plugins)):
            console.print(f"[dim]Skipping {name} (disabled)[/dim]")
    else:
        console.print("[red]Specify a source name or use --all[/red]")
        raise typer.Exit(1)

    for name in sources_to_sync:
        if name not in plugins:
            console.print(f"[red]Unknown source: {name}[/red]")
            continue

        plugin = plugins[name]
        plugin_config = config.get_plugin_config(name)

        console.print(f"[bold]Syncing {name}...[/bold]")

        with get_connection(config) as conn, sigterm_as_exit():
            store = SyncStore(conn, name)
            try:
                # A source named on its own waits for a running sync of it, as the Kobo
                # plug-in sync needs; --all (the timer) skips it
                run = SyncRun.start(
                    conn,
                    name,
                    store,
                    wait=_say_waiting(name) if source_name else None,
                )
            except SyncAlreadyRunning as e:
                console.print(f"[yellow]Skipping {name}: {e}[/yellow]")
                continue

            error_message = None

            with run:
                try:
                    store.run(plugin.sync(plugin_config))
                    if store.linked:
                        console.print(f"  [dim]Linked {store.linked} item(s) to their parent[/dim]")

                    # Resolve parent_id for LinkedIn reactions/comments
                    if name == "linkedin":
                        try:
                            from lestash_linkedin.source import (
                                download_linkedin_media,
                                resolve_linkedin_parents,
                            )

                            resolved = resolve_linkedin_parents(conn)
                            if resolved:
                                console.print(f"  [dim]Resolved {resolved} parent references[/dim]")

                            # Cache previews of posts you've engaged with (bounded per
                            # run so the sync stays quick; the rest catch up next run).
                            from lestash_linkedin.feed_preview import run_during_sync

                            run_during_sync(
                                conn,
                                plugin_config,
                                on_message=lambda msg: console.print(f"  [dim]{msg}[/dim]"),
                            )

                            # Try to download LinkedIn images
                            from lestash_linkedin.api import load_token

                            token = load_token()
                            if token and token.get("access_token"):
                                downloaded = download_linkedin_media(conn, token["access_token"])
                                if downloaded:
                                    console.print(
                                        f"  [dim]Downloaded {downloaded} LinkedIn image(s)[/dim]"
                                    )
                        except ImportError:
                            pass

                    # Resolve parent_id for YouTube transcripts captured before
                    # (or alongside) their video item.
                    if name == "youtube":
                        try:
                            from lestash_youtube.source import (
                                resolve_youtube_transcript_parents,
                            )

                            resolved = resolve_youtube_transcript_parents(conn)
                            if resolved:
                                console.print(
                                    f"  [dim]Resolved {resolved} transcript parent(s)[/dim]"
                                )
                        except ImportError:
                            pass

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

                run.finish(status, error=error_message)

        if status == "completed":
            console.print(f"[green]Synced {name}: {store.items} items added[/green]")


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

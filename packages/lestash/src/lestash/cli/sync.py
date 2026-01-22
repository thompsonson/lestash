"""Sync commands for Le Stash CLI.

These commands enable CRDT-based distributed sync using cr-sqlite.
Data can be exported to files for offline sync between devices.
"""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from lestash.core import crsqlite
from lestash.core.config import Config
from lestash.core.database import get_crdt_connection, get_db_path

app = typer.Typer(help="Distributed sync using cr-sqlite CRDTs.")
console = Console()

# Tables to upgrade to CRRs for sync
# Note: FTS tables and item_history are NOT synced (derived/local data)
SYNC_TABLES = ["items", "tags", "item_tags", "sources", "person_profiles"]


@app.command("setup")
def setup_sync(
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Re-download extension even if exists")
    ] = False,
) -> None:
    """Set up CRDT sync by downloading cr-sqlite and upgrading tables.

    This command:
    1. Downloads the cr-sqlite extension for your platform
    2. Converts tables to Conflict-free Replicated Relations (CRRs)

    After setup, use 'lestash sync export' and 'lestash sync import'
    to sync data between devices.
    """
    console.print("[bold]Setting up CRDT sync...[/bold]")

    # Step 1: Download extension
    console.print("\n[dim]Step 1: Downloading cr-sqlite extension...[/dim]")
    ext_path = crsqlite.download_extension(force=force)

    if ext_path is None:
        console.print("[red]Failed to download cr-sqlite extension.[/red]")
        console.print(
            "[dim]You can manually download from: "
            "https://github.com/vlcn-io/cr-sqlite/releases[/dim]"
        )
        raise typer.Exit(1)

    console.print(f"[green]✓ Extension downloaded to {ext_path}[/green]")

    # Step 2: Upgrade tables to CRRs
    console.print("\n[dim]Step 2: Upgrading tables to CRRs...[/dim]")

    config = Config.load()

    try:
        with get_crdt_connection(config) as conn:
            site_id = crsqlite.get_site_id(conn)
            console.print(f"[dim]Site ID: {site_id.hex() if site_id else 'unknown'}[/dim]")

            for table in SYNC_TABLES:
                if crsqlite.upgrade_to_crr(conn, table):
                    console.print(f"[green]✓ Upgraded '{table}' to CRR[/green]")
                else:
                    console.print(f"[yellow]⚠ Could not upgrade '{table}'[/yellow]")

            db_version = crsqlite.get_db_version(conn)
            console.print("\n[green]✓ Sync setup complete![/green]")
            console.print(f"[dim]Database version: {db_version}[/dim]")

    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command("status")
def sync_status() -> None:
    """Show sync status and database info."""
    config = Config.load()
    db_path = get_db_path(config)

    console.print(f"[bold]Database:[/bold] {db_path}")

    if not crsqlite.is_extension_available():
        console.print("[yellow]cr-sqlite extension: Not installed[/yellow]")
        console.print("[dim]Run 'lestash sync setup' to enable CRDT sync[/dim]")
        return

    console.print("[green]cr-sqlite extension: Installed[/green]")

    try:
        with get_crdt_connection(config) as conn:
            site_id = crsqlite.get_site_id(conn)
            db_version = crsqlite.get_db_version(conn)

            console.print(f"\n[bold]Site ID:[/bold] {site_id.hex() if site_id else 'unknown'}")
            console.print(f"[bold]Database version:[/bold] {db_version}")

            # Check CRR status for each table
            console.print("\n[bold]CRR Status:[/bold]")
            for table in SYNC_TABLES:
                try:
                    # Try to query changes for this table
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM crsql_changes WHERE \"table\" = ?",
                        (table,),
                    )
                    count = cursor.fetchone()[0]
                    console.print(f"  [green]✓[/green] {table} ({count} changes tracked)")
                except Exception:
                    console.print(f"  [yellow]○[/yellow] {table} (not a CRR)")

            # Show total changes
            cursor = conn.execute("SELECT COUNT(*) FROM crsql_changes")
            total_changes = cursor.fetchone()[0]
            console.print(f"\n[bold]Total changes tracked:[/bold] {total_changes}")

    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command("export")
def export_changes(
    output: Annotated[
        Path,
        typer.Argument(help="Output file path (JSON format)"),
    ],
    since: Annotated[
        int,
        typer.Option("--since", "-s", help="Only export changes after this version"),
    ] = 0,
) -> None:
    """Export changes to a JSON file for syncing to another device.

    The exported file contains all changes since a given version,
    which can be imported on another device using 'lestash sync import'.

    Example:
        lestash sync export ~/sync-export.json
        lestash sync export ~/sync-export.json --since 100
    """
    config = Config.load()

    if not crsqlite.is_extension_available():
        console.print("[red]cr-sqlite not installed. Run 'lestash sync setup' first.[/red]")
        raise typer.Exit(1)

    try:
        with get_crdt_connection(config) as conn:
            result = crsqlite.export_changes_to_file(conn, output, since_version=since)

            console.print(f"[green]✓ Exported {result['change_count']} changes[/green]")
            console.print(f"[dim]Site ID: {result['site_id']}[/dim]")
            console.print(f"[dim]DB Version: {result['db_version']}[/dim]")
            console.print(f"[dim]Output: {result['output_path']}[/dim]")

    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command("import")
def import_changes(
    input_file: Annotated[
        Path,
        typer.Argument(help="Input file path (JSON format from 'sync export')"),
    ],
    rebuild_fts: Annotated[
        bool,
        typer.Option("--rebuild-fts/--no-rebuild-fts", help="Rebuild FTS index after import"),
    ] = True,
) -> None:
    """Import changes from a JSON file exported from another device.

    This applies changes from another Le Stash instance, merging them
    with local data using CRDT conflict resolution.

    Example:
        lestash sync import ~/sync-export.json
    """
    config = Config.load()

    if not input_file.exists():
        console.print(f"[red]File not found: {input_file}[/red]")
        raise typer.Exit(1)

    if not crsqlite.is_extension_available():
        console.print("[red]cr-sqlite not installed. Run 'lestash sync setup' first.[/red]")
        raise typer.Exit(1)

    try:
        with get_crdt_connection(config) as conn:
            result = crsqlite.import_changes_from_file(conn, input_file)

            console.print(
                f"[green]✓ Applied {result['changes_applied']} of "
                f"{result['changes_received']} changes[/green]"
            )
            console.print(f"[dim]Source site: {result['source_site_id']}[/dim]")
            console.print(f"[dim]Source version: {result['source_db_version']}[/dim]")

            # Rebuild FTS index since cr-sqlite changes bypass triggers
            if rebuild_fts:
                console.print("\n[dim]Rebuilding full-text search index...[/dim]")
                count = crsqlite.rebuild_fts_index(conn)
                console.print(f"[green]✓ Re-indexed {count} items[/green]")

    except ValueError as e:
        console.print(f"[red]Invalid file format: {e}[/red]")
        raise typer.Exit(1) from None
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command("info")
def sync_info() -> None:
    """Show information about cr-sqlite and sync capabilities."""
    console.print("[bold]Le Stash CRDT Sync[/bold]\n")

    console.print(
        "Le Stash uses [cyan]cr-sqlite[/cyan] for distributed sync. "
        "This enables multi-master replication where changes from any device "
        "automatically merge without conflicts.\n"
    )

    console.print("[bold]How it works:[/bold]")
    console.print("1. Tables are converted to CRRs (Conflict-free Replicated Relations)")
    console.print("2. Each change is tracked with a logical clock (db_version)")
    console.print("3. Changes can be exported/imported as JSON files")
    console.print("4. CRDT merge algorithms resolve conflicts automatically\n")

    console.print("[bold]Supported tables:[/bold]")
    for table in SYNC_TABLES:
        console.print(f"  • {table}")

    console.print("\n[bold]Not synced (local only):[/bold]")
    console.print("  • items_fts (full-text search index - rebuilt after import)")
    console.print("  • item_history (local audit trail)")
    console.print("  • sync_log (local sync history)")
    console.print("  • log_entries (local logs)")

    console.print("\n[bold]Quick start:[/bold]")
    console.print("  1. lestash sync setup     # Install cr-sqlite and enable sync")
    console.print("  2. lestash sync export changes.json  # Export changes")
    console.print("  3. (transfer file to other device)")
    console.print("  4. lestash sync import changes.json  # Import on other device")

    console.print("\n[bold]Learn more:[/bold]")
    console.print("  • https://vlcn.io/docs/cr-sqlite/intro")
    console.print("  • https://github.com/vlcn-io/cr-sqlite")

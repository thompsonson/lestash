"""Database administration CLI commands."""

from __future__ import annotations

import json as json_mod
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lestash.core.db_admin import (
    BACKUP_GLOB,
    _format_bytes,
    count_fk_violations,
    count_history_before,
    create_backup,
    get_history_stats,
    get_status_summary,
    list_backups,
    prune_backups,
    purge_history,
    rebuild_fts,
    repair_foreign_keys,
    run_analyze_stats,
    run_full_analysis,
    run_full_integrity,
    run_pragma_optimize,
    run_vacuum,
    run_wal_checkpoint,
    verify_backup,
)

app = typer.Typer(help="Database administration and maintenance.", no_args_is_help=True)
backup_app = typer.Typer(help="Database backup management.", no_args_is_help=True)
app.add_typer(backup_app, name="backup")

console = Console()


def _get_conn_and_path():
    """Helper to get a connection and db_path."""
    from lestash.core.database import get_connection, get_db_path

    db_path = get_db_path()
    return get_connection(), db_path


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


@app.command()
def status(
    json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show database health dashboard."""
    from lestash.core.database import get_connection, get_db_path

    db_path = get_db_path()
    with get_connection() as conn:
        data = get_status_summary(conn, db_path)

    if json:
        console.print(json_mod.dumps(data, indent=2, default=str))
        return

    # --- File sizes ---
    fs = data["file_sizes"]
    pi = data["page_info"]
    console.print(
        Panel(
            f"[bold]{data['db_path']}[/bold]\n"
            f"Size: {_format_bytes(fs['total'])} "
            f"(db={_format_bytes(fs['db'])}, "
            f"wal={_format_bytes(fs['wal'])}, "
            f"shm={_format_bytes(fs['shm'])})\n"
            f"Schema v{data['schema_version']}  |  "
            f"Pages: {pi['page_count']:,} x {pi['page_size']}B  |  "
            f"Freelist: {pi['freelist_count']} pages ({_format_bytes(pi['freelist_bytes'])})",
            title="Database",
        )
    )

    # --- Tables ---
    if data["tables"]:
        t = Table(title="Tables", show_lines=False)
        t.add_column("Table", style="cyan")
        t.add_column("Rows", justify="right")
        t.add_column("Size", justify="right")
        for row in data["tables"]:
            t.add_row(row["name"], f"{row['rows']:,}", _format_bytes(row["size_bytes"]))
        console.print(t)

    # --- FTS ---
    fts = data["fts"]
    if fts["in_sync"]:
        status_str = "[green]in sync[/green]"
    else:
        status_str = f"[red]OUT OF SYNC[/red] (items={fts['items_count']}, fts={fts['fts_count']})"
    console.print(f"\n[bold]FTS5:[/bold] {status_str}")

    # --- Embeddings ---
    emb = data["embeddings"]
    if emb:
        console.print(
            f"[bold]Embeddings:[/bold] "
            f"{emb['embedded']}/{emb['total_parents']} parents "
            f"({emb['coverage']})"
        )
    else:
        console.print("[bold]Embeddings:[/bold] [dim]sqlite-vec not available[/dim]")

    # --- Sync health ---
    sync = data["sync"]
    if sync["stuck_syncs"]:
        console.print(f"\n[yellow]Stuck syncs ({len(sync['stuck_syncs'])}):[/yellow]")
        for s in sync["stuck_syncs"]:
            console.print(f"  {s['source_type']} — started {s['started_at']}")
    if sync["recent_failures"]:
        console.print(f"\n[yellow]Recent failures ({len(sync['recent_failures'])}):[/yellow]")
        for f in sync["recent_failures"]:
            console.print(f"  {f['source_type']} @ {f['started_at']}: {f['error_message']}")
    if not sync["stuck_syncs"] and not sync["recent_failures"]:
        console.print("\n[green]Sync: healthy[/green]")


# --------------------------------------------------------------------------- #
# integrity
# --------------------------------------------------------------------------- #


@app.command()
def integrity(
    json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run database integrity checks."""
    from lestash.core.database import get_connection

    with get_connection() as conn:
        report = run_full_integrity(conn)

    if json:
        console.print(json_mod.dumps(report, indent=2, default=str))
        return

    # Integrity check
    ic = report["integrity_check"]
    if ic == "ok":
        console.print("[green]PRAGMA integrity_check: ok[/green]")
    else:
        console.print(f"[red]PRAGMA integrity_check: {ic}[/red]")

    # Foreign key check
    fk = report["foreign_key_check"]
    if not fk:
        console.print("[green]PRAGMA foreign_key_check: ok (0 violations)[/green]")
    else:
        console.print(f"[red]PRAGMA foreign_key_check: {len(fk)} violations[/red]")
        for v in fk[:10]:
            console.print(f"  table={v['table']} rowid={v['rowid']} parent={v['parent']}")
        if len(fk) > 10:
            console.print(f"  ... and {len(fk) - 10} more")

    # FTS integrity
    fi = report["fts_integrity"]
    if fi == "ok":
        console.print("[green]FTS5 integrity-check: ok[/green]")
    else:
        console.print(f"[red]FTS5 integrity-check: {fi}[/red]")

    # WAL mode
    if report["wal_mode"]:
        console.print("[green]WAL mode: active[/green]")
    else:
        console.print("[red]WAL mode: inactive[/red]")

    # Foreign keys pragma
    if report["foreign_keys_enabled"]:
        console.print("[green]Foreign keys: enabled[/green]")
    else:
        console.print("[yellow]Foreign keys: disabled (enabled per-connection by app)[/yellow]")

    # Orphans
    for label, key in [
        ("Orphaned children (missing parent)", "orphaned_children"),
        ("Orphaned media (missing item)", "orphaned_media"),
        ("Orphaned tags (no items)", "orphaned_tags"),
    ]:
        items = report[key]
        if not items:
            console.print(f"[green]{label}: 0[/green]")
        else:
            console.print(f"[yellow]{label}: {len(items)}[/yellow]")


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #


@app.command()
def repair(
    confirm: bool = typer.Option(False, "--confirm", help="Skip confirmation prompt."),
) -> None:
    """Clean up orphaned rows that violate foreign key constraints."""
    from lestash.core.database import get_connection

    with get_connection() as conn:
        violations = count_fk_violations(conn)

    if not violations:
        console.print("[green]No FK violations found.[/green]")
        return

    total = sum(violations.values())
    console.print(f"[yellow]Found {total} orphaned rows:[/yellow]")
    for table, count in violations.items():
        console.print(f"  {table}: {count}")

    if not confirm:
        typer.confirm("Delete these orphaned rows?", abort=True)

    with get_connection() as conn:
        deleted = repair_foreign_keys(conn)

    console.print("\n[green]Repaired:[/green]")
    for table, count in deleted.items():
        console.print(f"  {table}: {count} rows deleted")


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #


@app.command()
def analyze(
    json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Deep database analysis."""
    from lestash.core.database import get_connection

    with get_connection() as conn:
        data = run_full_analysis(conn)

    if json:
        console.print(json_mod.dumps(data, indent=2, default=str))
        return

    # Source distribution
    t = Table(title="Source Distribution", show_lines=False)
    t.add_column("Source", style="cyan")
    t.add_column("Total", justify="right")
    t.add_column("Parents", justify="right")
    t.add_column("Children", justify="right")
    t.add_column("Content Size", justify="right")
    for row in data["source_distribution"]:
        t.add_row(
            row["source_type"],
            f"{row['count']:,}",
            f"{row['parents']:,}",
            f"{row['children']:,}",
            _format_bytes(row["total_content_size"] or 0),
        )
    console.print(t)

    # Largest items
    t = Table(title="Largest Items (by content)", show_lines=False)
    t.add_column("ID", justify="right")
    t.add_column("Source", style="cyan")
    t.add_column("Title")
    t.add_column("Size", justify="right")
    for row in data["largest_items"][:10]:
        title = (row["title"] or "")[:50]
        t.add_row(str(row["id"]), row["source_type"], title, _format_bytes(row["content_size"]))
    console.print(t)

    # Duplicates
    dupes = data["duplicates"]
    if dupes:
        console.print(f"\n[yellow]Duplicate source_ids: {len(dupes)}[/yellow]")
        for d in dupes[:5]:
            console.print(f"  {d['source_type']}/{d['source_id']} x{d['cnt']}")
    else:
        console.print("\n[green]No duplicate source_ids[/green]")

    # FTS consistency
    fts = data["fts"]
    if fts["in_sync"]:
        console.print(f"[green]FTS5: in sync ({fts['items_count']} items)[/green]")
    else:
        console.print(
            f"[red]FTS5: OUT OF SYNC (items={fts['items_count']}, fts={fts['fts_count']})[/red]"
        )

    # Orphans summary
    for label, key in [
        ("Orphaned children", "orphaned_children"),
        ("Orphaned media", "orphaned_media"),
        ("Orphaned tags", "orphaned_tags"),
    ]:
        count = len(data[key])
        if count:
            console.print(f"[yellow]{label}: {count}[/yellow]")
        else:
            console.print(f"[green]{label}: 0[/green]")

    # Embedding coverage by source
    ecbs = data["embedding_coverage_by_source"]
    if ecbs:
        t = Table(title="Embedding Coverage by Source", show_lines=False)
        t.add_column("Source", style="cyan")
        t.add_column("Total", justify="right")
        t.add_column("Embedded", justify="right")
        t.add_column("Coverage", justify="right")
        for row in ecbs:
            pct = f"{row['embedded'] / row['total'] * 100:.0f}%" if row["total"] else "N/A"
            t.add_row(row["source_type"], str(row["total"]), str(row["embedded"]), pct)
        console.print(t)


# --------------------------------------------------------------------------- #
# optimize
# --------------------------------------------------------------------------- #


@app.command()
def optimize(
    all: bool = typer.Option(False, "--all", help="Run all optimizations."),
    vacuum: bool = typer.Option(False, "--vacuum", help="VACUUM the database."),
    fts: bool = typer.Option(False, "--fts", help="Rebuild FTS5 index."),
    checkpoint: bool = typer.Option(False, "--checkpoint", help="WAL checkpoint (TRUNCATE)."),
    stats: bool = typer.Option(False, "--stats", help="Run ANALYZE + PRAGMA optimize."),
    confirm: bool = typer.Option(False, "--confirm", help="Skip confirmation prompt."),
) -> None:
    """Run database maintenance operations."""
    from lestash.core.database import get_connection, get_db_path

    if not any([all, vacuum, fts, checkpoint, stats]):
        console.print(
            "Specify at least one operation (--all, --vacuum, --fts, --checkpoint, --stats)."
        )
        console.print("Run [bold]lestash db optimize --help[/bold] for details.")
        raise typer.Exit(1)

    do_vacuum = all or vacuum
    do_fts = all or fts
    do_checkpoint = all or checkpoint
    do_stats = all or stats

    if do_vacuum and not confirm:
        typer.confirm("VACUUM rewrites the entire database. Continue?", abort=True)

    db_path = get_db_path()

    # Checkpoint first (before vacuum, to fold WAL into main DB)
    if do_checkpoint:
        with get_connection() as conn:
            result = run_wal_checkpoint(conn)
        console.print(
            f"[green]WAL checkpoint:[/green] "
            f"log={result['log']}, checkpointed={result['checkpointed']}"
        )

    if do_vacuum:
        result = run_vacuum(db_path)
        console.print(
            f"[green]VACUUM:[/green] {_format_bytes(result['size_before'])} -> "
            f"{_format_bytes(result['size_after'])} (freed {_format_bytes(result['freed'])})"
        )

    if do_fts:
        with get_connection() as conn:
            rebuild_fts(conn)
        console.print("[green]FTS5 rebuild: done[/green]")

    if do_stats:
        with get_connection() as conn:
            run_analyze_stats(conn)
            run_pragma_optimize(conn)
        console.print("[green]ANALYZE + PRAGMA optimize: done[/green]")


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #


@backup_app.command("create")
def backup_create() -> None:
    """Create a timestamped database backup."""
    from lestash.core.database import get_db_path

    db_path = get_db_path()
    dest = create_backup(db_path)
    size = dest.stat().st_size
    console.print(f"[green]Backup created:[/green] {dest.name} ({_format_bytes(size)})")


@backup_app.command("list")
def backup_list() -> None:
    """List managed backups."""
    from lestash.core.database import get_db_path

    backups = list_backups(get_db_path())
    if not backups:
        console.print(f"No managed backups found (pattern: {BACKUP_GLOB}).")
        return

    t = Table(title="Managed Backups", show_lines=False)
    t.add_column("Filename")
    t.add_column("Size", justify="right")
    t.add_column("Modified")
    for b in backups:
        t.add_row(b["filename"], _format_bytes(b["size_bytes"]), b["modified"])
    console.print(t)


@backup_app.command("verify")
def backup_verify(
    filename: str = typer.Argument(
        None, help="Backup filename to verify. Defaults to most recent."
    ),
) -> None:
    """Verify a backup's integrity."""
    from lestash.core.database import get_db_path

    db_path = get_db_path()
    if filename:
        backup_path = db_path.parent / filename
    else:
        backups = list_backups(db_path)
        if not backups:
            console.print("No managed backups found.")
            raise typer.Exit(1)
        backup_path = Path(backups[0]["path"])

    console.print(f"Verifying {backup_path.name}...")
    result = verify_backup(backup_path)

    if result["valid"]:
        console.print(
            f"[green]Valid:[/green] schema v{result['schema_version']}, "
            f"{result['item_count']:,} items"
        )
    else:
        err = result.get("error", result.get("integrity", "unknown"))
        console.print(f"[red]Invalid:[/red] {err}")


@backup_app.command("prune")
def backup_prune(
    keep: int = typer.Option(5, "--keep", help="Number of backups to keep."),
    confirm: bool = typer.Option(False, "--confirm", help="Skip confirmation prompt."),
) -> None:
    """Delete old managed backups, keeping the newest N."""
    from lestash.core.database import get_db_path

    db_path = get_db_path()
    backups = list_backups(db_path)
    to_delete_count = max(0, len(backups) - keep)

    if to_delete_count == 0:
        console.print(f"Nothing to prune ({len(backups)} backups, keeping {keep}).")
        return

    if not confirm:
        typer.confirm(f"Delete {to_delete_count} backup(s)?", abort=True)

    deleted = prune_backups(db_path, keep=keep)
    console.print(f"[green]Pruned {len(deleted)} backup(s).[/green]")


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


@app.command()
def history(
    purge: bool = typer.Option(False, "--purge", help="Purge old history records."),
    before: str = typer.Option(None, "--before", help="Delete records before date (YYYY-MM-DD)."),
    keep_days: int = typer.Option(None, "--keep-days", help="Keep only the last N days."),
    confirm: bool = typer.Option(False, "--confirm", help="Skip confirmation prompt."),
    json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show item history stats or purge old records."""
    from lestash.core.database import get_connection

    with get_connection() as conn:
        if purge:
            if not before and keep_days is None:
                console.print("Specify --before DATE or --keep-days N with --purge.")
                raise typer.Exit(1)

            count = count_history_before(conn, before=before, keep_days=keep_days)
            if count == 0:
                console.print("No records match the criteria.")
                return

            if not confirm:
                typer.confirm(f"Delete {count:,} history records?", abort=True)

            deleted = purge_history(conn, before=before, keep_days=keep_days)
            console.print(f"[green]Purged {deleted:,} history records.[/green]")
            return

        stats = get_history_stats(conn)

    if json:
        console.print(json_mod.dumps(stats, indent=2, default=str))
        return

    console.print("[bold]Item History[/bold]")
    console.print(f"  Total records: {stats['total_records']:,}")
    if stats["size_bytes"]:
        console.print(f"  Size: {_format_bytes(stats['size_bytes'])}")
    if stats["earliest"]:
        console.print(f"  Range: {stats['earliest']} — {stats['latest']}")
    if stats["by_type"]:
        for bt in stats["by_type"]:
            console.print(f"  {bt['change_type']}: {bt['count']:,}")

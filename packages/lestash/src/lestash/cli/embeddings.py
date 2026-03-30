"""Embeddings CLI commands — status and rebuild."""

import typer
from rich.console import Console
from rich.progress import Progress

app = typer.Typer(help="Vector embeddings for semantic search.", no_args_is_help=True)
console = Console()


@app.command()
def status() -> None:
    """Show embedding coverage statistics."""
    from lestash.core.database import get_connection
    from lestash.core.embeddings import ensure_vec_table, get_embedding_stats, load_vec_extension

    with get_connection() as conn:
        load_vec_extension(conn)
        ensure_vec_table(conn)
        stats = get_embedding_stats(conn)

    console.print("[bold]Embedding Status[/bold]\n")
    console.print(f"  Model: {stats['model']}")
    console.print(f"  Dimensions: {stats['dimensions']}")
    console.print(f"  Embedded: {stats['embedded']} / {stats['total_parents']} parents")
    console.print(f"  Coverage: {stats['coverage']}")


@app.command()
def rebuild() -> None:
    """Embed all parent items missing vectors."""
    from lestash.core.database import get_connection
    from lestash.core.embeddings import load_vec_extension, rebuild_embeddings

    console.print("Loading embedding model...")

    with get_connection() as conn:
        load_vec_extension(conn)

        with Progress(console=console) as progress:
            task = progress.add_task("Embedding items...", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task, completed=done, total=total)

            count = rebuild_embeddings(conn, progress_callback=on_progress)

    if count:
        console.print(f"\n[green]✓[/green] Embedded {count} items.")
    else:
        console.print("\n[green]✓[/green] All parent items already have embeddings.")

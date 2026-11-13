"""Embeddings CLI commands — status, refresh and rebuild."""

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
    console.print(f"  Recipe: {stats['recipe']}")
    console.print(f"  Dimensions: {stats['dimensions']}")
    console.print(f"  Embedded: {stats['embedded']} / {stats['total_parents']} parents")
    console.print(f"  Coverage: {stats['coverage']}")
    console.print(f"  Not from this model and recipe: {stats['out_of_date']}")


@app.command()
def refresh(
    budget: float | None = typer.Option(
        None, "--budget", help="Stop after this many seconds; the rest wait for the next run"
    ),
) -> None:
    """Embed new and changed items, and drop vectors of items gone (#221).
    Runs after each timer sync, once text from files is read."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection
    from lestash.core.embeddings import load_vec_extension, refresh_embeddings

    config = Config.load()
    with get_connection(config) as conn:
        load_vec_extension(conn)
        counts = refresh_embeddings(conn, budget_s=budget)
    console.print(
        f"Embedded {counts.embedded} item(s), removed {counts.removed} vector(s); "
        f"{counts.pending} left for next time."
    )
    if counts.stopped:
        console.print("[yellow]Stopped: the budget ran out. The next run goes on.[/yellow]")


@app.command()
def rebuild() -> None:
    """Embed every item that is new or changed, with no time limit."""
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
        console.print("\n[green]✓[/green] Every item's vector is up to date.")

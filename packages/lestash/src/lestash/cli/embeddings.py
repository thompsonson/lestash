"""Embeddings CLI commands — status, refresh and rebuild."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.progress import Progress

app = typer.Typer(help="Vector embeddings for semantic search.", no_args_is_help=True)
console = Console()

if TYPE_CHECKING:
    from lestash.core.embeddings import RefreshCounts


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
    if stats["building"]:
        console.print(
            f"  [yellow]Building its index; search uses {stats['query_model']} until"
            " a whole refresh finishes.[/yellow]"
        )
    console.print(f"  Recipe: {stats['recipe']}")
    console.print(f"  Dimensions: {stats['dimensions']}")
    console.print(f"  Embedded: {stats['embedded']} / {stats['total_parents']} parents")
    console.print(f"  Coverage: {stats['coverage']}")
    console.print(f"  Parts embedded: {stats['parts_embedded']} (rounds, comments, highlights...)")
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
    _report(counts)


def _report(counts: RefreshCounts) -> None:
    from lestash.core.embeddings import MODEL_NAME

    rate = f", {counts.embedded / counts.seconds:.1f}/s" if counts.seconds else ""
    console.print(
        f"Embedded {counts.embedded} item(s) with {MODEL_NAME} in {counts.seconds:.0f}s"
        f"{rate}, removed {counts.removed} vector(s); {counts.pending} left for next time."
    )
    if counts.stopped:
        console.print("[yellow]Stopped: the budget ran out. The next run goes on.[/yellow]")
    if counts.swapped:
        console.print(f"[green]✓[/green] Search now uses the new index: {counts.swapped} vectors.")


@app.command()
def rebuild(
    host: str | None = typer.Option(
        None,
        "--host",
        help="Embed on this machine over ssh; default [embeddings] host. 'local' for here",
    ),
    threads: int | None = typer.Option(None, "--threads", help="Threads on the host"),
) -> None:
    """Embed every item that is new or changed, with no time limit: on the host named
    by [embeddings] host if it can be reached, else here."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection
    from lestash.core.embed_remote import HostUnavailable, rebuild_on
    from lestash.core.embeddings import load_vec_extension, refresh_embeddings

    config = Config.load()
    settings = config.get_plugin_config("embeddings")
    host = host or settings.get("host")
    if host == "local":
        host = None
    threads = threads or int(settings.get("threads", 16))

    with get_connection(config) as conn:
        load_vec_extension(conn)

        with Progress(console=console) as progress:
            task = progress.add_task("Embedding items...", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task, completed=done, total=total)

            counts = None
            if host:
                try:
                    counts = rebuild_on(conn, host, threads=threads, progress_callback=on_progress)
                except HostUnavailable as e:
                    console.print(f"[yellow]{e}; embedding here instead.[/yellow]")
            if counts is None:
                counts = refresh_embeddings(conn, progress_callback=on_progress)

    _report(counts)

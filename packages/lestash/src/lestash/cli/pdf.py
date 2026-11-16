"""PDFs you add, and their versions (#338)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from lestash.core.config import Config
from lestash.core.database import get_connection

app = typer.Typer(help="PDFs you add, and their versions.")
console = Console()


def _collection_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
    if row is None:
        console.print(f"[red]No collection named {name!r}.[/red]")
        raise typer.Exit(1)
    return row[0]


@app.command("add")
def add_cmd(
    files: Annotated[list[Path], typer.Argument(help="PDF files to add", exists=True)],
    collection: Annotated[
        str | None, typer.Option("--collection", "-c", help="Add each to this collection")
    ] = None,
    revision_of: Annotated[
        int | None,
        typer.Option("--revision-of", help="The item this file is the next version of"),
    ] = None,
    extract: Annotated[
        bool, typer.Option("--extract/--no-extract", help="Extract the text for search")
    ] = True,
) -> None:
    """Add PDFs as items. A file already held is not added again."""
    from lestash.core.collections import add_item
    from lestash.core.pdfs import VersionError, add_pdf, link_revision, older_problem

    if revision_of is not None and len(files) != 1:
        console.print("[red]--revision-of takes exactly one file.[/red]")
        raise typer.Exit(1)
    for path in files:
        if path.suffix.lower() != ".pdf" or not path.is_file():
            console.print(f"[red]{path} is not a PDF file.[/red]")
            raise typer.Exit(1)
    config = Config.load()
    new_ids: list[int] = []
    with get_connection(config) as conn:
        cid = _collection_id(conn, collection) if collection else None
        if revision_of is not None:
            problem = older_problem(conn, revision_of)
            if problem:
                console.print(f"[red]{problem}. Nothing added.[/red]")
                raise typer.Exit(1)
        for path in files:
            added = add_pdf(conn, config, path.read_bytes(), path.name, extract=extract)
            if added.new:
                new_ids.append(added.item_id)
            if revision_of is not None:
                try:
                    link_revision(conn, added.item_id, revision_of)
                except VersionError as e:
                    conn.rollback()
                    console.print(f"[red]{path.name}: {e}. Nothing added.[/red]")
                    raise typer.Exit(1) from None
            if cid is not None:
                add_item(conn, cid, added.item_id)
            conn.commit()
            word = "added" if added.new else "already held"
            console.print(f"{path.name}: {word}, item {added.item_id}")
    if new_ids:
        from lestash.core.pdf_enrich import enrich_item

        for item_id in new_ids:
            result = enrich_item(item_id, config=config)
            if result.status == "failed":
                console.print(f"[yellow]Item {item_id}: enrich failed: {result.message}[/yellow]")


@app.command("revision")
def revision_cmd(
    newer: Annotated[int, typer.Argument(help="The newer version's item id")],
    older: Annotated[int, typer.Argument(help="The version before it")],
) -> None:
    """Record that one item is the next version of another."""
    from lestash.core.pdfs import VersionError, link_revision

    with get_connection(Config.load()) as conn:
        try:
            made = link_revision(conn, newer, older)
        except VersionError as e:
            console.print(f"[red]{e}.[/red]")
            raise typer.Exit(1) from None
        conn.commit()
    console.print(
        f"Item {newer} is the next version of item {older}"
        + ("." if made else " (already recorded).")
    )


@app.command("versions")
def versions_cmd(item: Annotated[int, typer.Argument(help="Any version's item id")]) -> None:
    """List every version of a document, newest first."""
    from lestash.core.pdfs import versions

    with get_connection(Config.load()) as conn:
        if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item,)).fetchone():
            console.print(f"[red]Item {item} doesn't exist.[/red]")
            raise typer.Exit(1)
        for item_id in versions(conn, item):
            title, created = conn.execute(
                "SELECT title, created_at FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            mark = "*" if item_id == item else " "
            console.print(f"{mark} {item_id}  {str(created)[:10]}  {title}")

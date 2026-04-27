"""PDF enrichment CLI commands.

lestash enrich item N            # enrich a single item
lestash enrich all               # enrich every PDF item
lestash enrich backfill-sources  # one-shot legacy source-PDF backfill
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.progress import Progress

app = typer.Typer(
    help="PDF enrichment pipeline (links, images, ink annotations).", no_args_is_help=True
)
console = Console()


@app.command("item")
def enrich_one(
    item_id: int = typer.Argument(..., help="Item ID to enrich"),
    force: bool = typer.Option(False, "--force", help="Re-run even if already enriched"),
) -> None:
    """Enrich a single PDF item."""
    from lestash.core.pdf_enrich import enrich_item

    result = enrich_item(item_id, force=force)
    _print_result(result)


@app.command("all")
def enrich_all(
    force: bool = typer.Option(False, "--force", help="Re-run even when already enriched"),
) -> None:
    """Enrich every PDF item in the database. Idempotent — skips items whose
    stored extractor_version + sha256 already matches."""
    from lestash.core.database import get_connection
    from lestash.core.pdf_enrich import enrich_item, list_pdf_items

    with get_connection() as conn:
        ids = list_pdf_items(conn)

    if not ids:
        console.print("[yellow]No PDF items found.[/yellow]")
        return

    counts = {"enriched": 0, "skipped": 0, "source_unavailable": 0, "failed": 0}

    with Progress(console=console) as progress:
        task = progress.add_task("Enriching", total=len(ids))
        for item_id in ids:
            result = enrich_item(item_id, force=force)
            counts[result.status] = counts.get(result.status, 0) + 1
            progress.update(task, advance=1)

    console.print(
        f"\n[bold]Done.[/bold] enriched={counts['enriched']} "
        f"skipped={counts['skipped']} unavailable={counts['source_unavailable']} "
        f"failed={counts['failed']}"
    )


@app.command("ocr")
def ocr(
    item_id: int | None = typer.Option(
        None, "--item-id", help="Limit OCR to children of this parent (default: all)"
    ),
) -> None:
    """Transcribe handwritten margin notes and unclassified ink via Claude
    multimodal vision. Requires ANTHROPIC_API_KEY.

    Idempotent: child items already OCR'd at the current ocr_version are
    skipped.
    """
    from lestash.core.pdf_enrich import ocr_pending_annotations

    results = ocr_pending_annotations(item_id=item_id)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    if not results:
        console.print("[blue]No annotations needing OCR.[/blue]")
        return
    console.print(
        f"transcribed {counts.get('transcribed', 0)}, "
        f"skipped {counts.get('skipped', 0)}, "
        f"unavailable {counts.get('unavailable', 0)}, "
        f"failed {counts.get('failed', 0)}."
    )


@app.command("backfill-sources")
def backfill_sources() -> None:
    """One-shot: download any missing source PDFs from Google Drive and persist
    them as `source_pdf` media rows. Required for items imported before the
    enrichment pipeline existed.

    Idempotent — items that already have a source_pdf row are skipped.
    """
    from lestash.core.pdf_enrich import backfill_source_pdfs

    stats = backfill_source_pdfs()
    console.print(
        f"Inspected {stats.inspected} items: "
        f"backfilled {stats.backfilled}, "
        f"already-present {stats.already_present}, "
        f"unavailable {stats.unavailable}."
    )


def _print_result(result) -> None:
    colours = {
        "enriched": "green",
        "skipped": "blue",
        "source_unavailable": "yellow",
        "failed": "red",
    }
    colour = colours.get(result.status, "white")
    console.print(
        f"[{colour}]{result.status}[/{colour}] item={result.item_id} "
        f"images={result.images} annotations={result.annotations}"
    )
    if result.message:
        console.print(f"  {result.message}")

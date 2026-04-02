"""Audible source plugin implementation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_audible.client import (
    build_login_url,
    complete_auth,
    extract_book_metadata,
    get_bookmarks,
    get_chapters,
    get_client,
    get_library,
    is_authenticated,
    load_config,
)

console = Console()
logger = get_plugin_logger("audible")

# Audible marketplace options
MARKETPLACES = ["us", "uk", "de", "fr", "au", "ca", "jp", "it", "in", "es"]


def format_position(position_ms: int) -> str:
    """Format millisecond position as HH:MM:SS."""
    total_seconds = position_ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def book_to_item(
    book: dict[str, Any],
    chapters: list[dict[str, Any]] | None = None,
) -> ItemCreate:
    """Convert an Audible library book to an ItemCreate."""
    meta = extract_book_metadata(book)
    asin = meta["asin"]
    title = meta["title"]
    authors_str = ", ".join(meta["authors"]) if meta["authors"] else "Unknown"

    # Use publisher description as content (searchable), fall back to formatted info
    content = meta.get("description", "")
    if not content:
        subtitle = f" — {meta['subtitle']}" if meta.get("subtitle") else ""
        content = f"{title}{subtitle}\nBy: {authors_str}"

    created_at = None
    if meta.get("release_date"):
        with suppress(ValueError):
            created_at = datetime.strptime(meta["release_date"], "%Y-%m-%d")

    book_meta: dict[str, Any] = {"type": "book", **meta}
    if chapters:
        book_meta["chapters"] = chapters

    return ItemCreate(
        source_type="audible",
        source_id=f"audible:book:{asin}",
        title=title,
        content=content,
        author=authors_str,
        created_at=created_at,
        url=f"https://www.audible.com/pd/{asin}",
        is_own_content=False,
        metadata=book_meta,
    )


def _find_chapter(position_ms: int, chapters: list[dict[str, Any]]) -> str | None:
    """Find the chapter title for a given position in milliseconds."""
    for ch in chapters:
        start = ch.get("start_ms", 0)
        length = ch.get("length_ms", 0)
        if start <= position_ms < start + length:
            return ch.get("title")
    return None


def _deduplicate_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate annotations by startPosition.

    Audible creates multiple records per annotation (bookmark + clip + note).
    We keep the richest record for each position — prefer notes over clips
    over bookmarks.
    """
    by_position: dict[str, dict[str, Any]] = {}
    type_priority = {"audible.note": 3, "audible.clip": 2, "audible.bookmark": 1}

    for a in annotations:
        pos = a.get("startPosition", "0")
        priority = type_priority.get(a.get("type", ""), 0)
        existing = by_position.get(pos)
        if not existing or priority > type_priority.get(existing.get("type", ""), 0):
            by_position[pos] = a

    return list(by_position.values())


def _get_note_text(record: dict[str, Any]) -> str:
    """Extract note text from a sidecar record.

    Notes store text in 'text' field, clips in 'metadata.note'.
    """
    text = record.get("text", "")
    if not text:
        text = (record.get("metadata") or {}).get("note", "")
    return text.strip() if text else ""


def bookmark_to_item(
    bookmark: dict[str, Any],
    book_meta: dict[str, Any],
    chapters: list[dict[str, Any]] | None = None,
) -> ItemCreate | None:
    """Convert an Audible bookmark/note to an ItemCreate.

    Args:
        bookmark: Bookmark dict from sidecar endpoint.
        book_meta: Book metadata from extract_book_metadata().
        chapters: Optional chapter list for resolving position → chapter name.

    Returns:
        ItemCreate or None if bookmark has no useful content.
    """
    asin = book_meta["asin"]
    book_title = book_meta["title"]
    position_ms = int(bookmark.get("startPosition", 0))
    note_text = _get_note_text(bookmark)
    annotation_type = bookmark.get("type", "audible.bookmark")
    annotation_id = bookmark.get("annotationId", str(position_ms))
    created_at_str = bookmark.get("creationTime")

    # Use annotationId for stable dedup
    source_id = f"audible:annotation:{asin}:{annotation_id}"

    # Format the position for display
    position_str = format_position(position_ms)

    # Resolve chapter
    chapter_name = _find_chapter(position_ms, chapters) if chapters else None

    # Build title with chapter context
    location = f" — {chapter_name}" if chapter_name else ""
    if note_text:
        content = note_text
        title = f"Note in {book_title}{location} at {position_str}"
    else:
        content = f"Bookmark at {position_str}"
        title = f"Bookmark in {book_title}{location} at {position_str}"

    created_at = None
    if created_at_str:
        with suppress(ValueError, AttributeError):
            # Audible uses "2025-03-22 22:26:53.0" format
            created_at = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S.%f")

    item_meta: dict[str, Any] = {
        "type": annotation_type,
        "asin": asin,
        "book_title": book_title,
        "position_ms": position_ms,
        "position_str": position_str,
        "_parent_source_id": f"audible:book:{asin}",
    }
    if chapter_name:
        item_meta["chapter"] = chapter_name

    return ItemCreate(
        source_type="audible",
        source_id=source_id,
        title=title,
        content=content,
        author=", ".join(book_meta["authors"]) if book_meta["authors"] else None,
        created_at=created_at,
        url=f"https://www.audible.com/pd/{asin}",
        is_own_content=True,
        metadata=item_meta,
    )


class AudibleSource(SourcePlugin):
    """Audible audiobook bookmarks and notes source."""

    name = "audible"
    description = "Import bookmarks and notes from Audible audiobooks"

    def get_commands(self) -> typer.Typer:
        """Return Audible CLI commands."""
        app = typer.Typer(help="Audible audiobook bookmarks and notes.")

        @app.command("auth")
        def auth(
            locale: Annotated[
                str, typer.Option(help=f"Marketplace ({', '.join(MARKETPLACES)})")
            ] = "us",
        ) -> None:
            """Authenticate with Audible via browser.

            Opens the Amazon login page in your browser. After signing in,
            copy the URL from the address bar and paste it here.
            """
            import webbrowser

            from rich.prompt import Prompt

            if locale not in MARKETPLACES:
                valid = ", ".join(MARKETPLACES)
                console.print(f"[red]Invalid locale '{locale}'. Use one of: {valid}[/red]")
                raise typer.Exit(1)

            try:
                state = build_login_url(locale=locale)
                console.print("[bold]Opening Amazon login in browser...[/bold]")
                webbrowser.open(state["url"])
                console.print(
                    "\nAfter signing in, copy the URL from your browser's"
                    " address bar and paste it below."
                )
                redirect_url = Prompt.ask("URL")
                complete_auth(redirect_url, state)
                console.print("[green]Authenticated and credentials saved.[/green]")
            except Exception as e:
                console.print(f"[red]Authentication failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("doctor")
        def doctor() -> None:
            """Check Audible configuration and connectivity."""
            if not is_authenticated():
                console.print("[red]Not authenticated. Run 'lestash audible auth' first.[/red]")
                raise typer.Exit(1)

            config = load_config()
            console.print(f"[green]Authenticated[/green] (locale: {config.get('locale', 'us')})")

            client = get_client()
            if not client:
                console.print("[red]Failed to create API client.[/red]")
                raise typer.Exit(1)

            with client:
                try:
                    books = get_library(client)
                    console.print(f"[green]Library accessible: {len(books)} books[/green]")

                    # Show a few books as sample
                    if books:
                        console.print("\n[bold]Sample books:[/bold]")
                        for book in books[:5]:
                            meta = extract_book_metadata(book)
                            authors = ", ".join(meta["authors"][:2])
                            console.print(f"  {meta['title']} — {authors}")
                except Exception as e:
                    console.print(f"[red]API error: {e}[/red]")
                    raise typer.Exit(1) from None

        @app.command("library")
        def library() -> None:
            """List books in your Audible library."""
            client = get_client()
            if not client:
                console.print("[red]Not authenticated. Run 'lestash audible auth' first.[/red]")
                raise typer.Exit(1)

            with client:
                books = get_library(client)

            table = Table(title=f"Audible Library ({len(books)} books)")
            table.add_column("ASIN", style="dim", max_width=12)
            table.add_column("Title", max_width=40)
            table.add_column("Author", max_width=25)
            table.add_column("Length", justify="right", max_width=8)

            for book in books:
                meta = extract_book_metadata(book)
                authors = ", ".join(meta["authors"][:2]) if meta["authors"] else "-"
                runtime = ""
                if meta.get("runtime_minutes"):
                    h, m = divmod(meta["runtime_minutes"], 60)
                    runtime = f"{h}h {m}m" if h else f"{m}m"
                table.add_row(meta["asin"], meta["title"], authors, runtime)

            console.print(table)

        @app.command("fetch")
        def fetch(
            asin: Annotated[
                str | None, typer.Option(help="Fetch bookmarks for a specific book ASIN")
            ] = None,
        ) -> None:
            """Fetch books and their bookmarks/notes from Audible."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection, upsert_item

            client = get_client()
            if not client:
                console.print("[red]Not authenticated. Run 'lestash audible auth' first.[/red]")
                raise typer.Exit(1)

            config = Config.load()
            total_books = 0
            total_bookmarks = 0

            with client:
                if asin:
                    # Fetch single book's bookmarks
                    books = [b for b in get_library(client) if b.get("asin") == asin]
                    if not books:
                        console.print(f"[red]Book with ASIN {asin} not found in library.[/red]")
                        raise typer.Exit(1)
                else:
                    books = get_library(client)

                with get_connection(config) as conn:
                    for book in books:
                        meta = extract_book_metadata(book)
                        book_asin = meta["asin"]

                        # Fetch bookmarks for this book
                        records = get_bookmarks(client, book_asin)
                        annotations = _extract_annotations(records)
                        if not annotations:
                            continue

                        # Fetch chapters for context
                        chapters = get_chapters(client, book_asin)

                        # Insert book as parent (with chapters in metadata)
                        book_item = book_to_item(book, chapters=chapters)
                        book_id = upsert_item(conn, book_item)
                        total_books += 1

                        # Insert bookmarks as children (with chapter context)
                        for annotation in annotations:
                            bm_item = bookmark_to_item(annotation, meta, chapters=chapters)
                            if bm_item:
                                bm_item.parent_id = book_id
                                if bm_item.metadata:
                                    bm_item.metadata.pop("_parent_source_id", None)
                                upsert_item(conn, bm_item)
                                total_bookmarks += 1

                    conn.commit()

            console.print(
                f"[green]Imported {total_bookmarks} bookmarks/notes"
                f" from {total_books} books[/green]"
            )

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync Audible bookmarks and notes.

        Yields book items and their bookmark/note children.
        """
        client = get_client()
        if not client:
            logger.warning("No Audible credentials found for sync")
            return

        with client:
            books = get_library(client)

            for book in books:
                meta = extract_book_metadata(book)
                asin = meta["asin"]

                records = get_bookmarks(client, asin)
                annotations = _extract_annotations(records)
                if not annotations:
                    continue

                # Fetch chapters for context
                chapters = get_chapters(client, asin)

                # Yield book as parent (with chapters)
                yield book_to_item(book, chapters=chapters)

                # Yield bookmarks as children (with chapter context)
                for annotation in annotations:
                    item = bookmark_to_item(annotation, meta, chapters=chapters)
                    if item:
                        yield item

    def configure(self) -> dict:
        """Interactive configuration."""
        return {"locale": "us"}


def _extract_annotations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter and deduplicate sidecar records to user annotations.

    Excludes system records (audible.last_heard) and deduplicates by position
    (Audible creates bookmark + clip + note records for the same annotation).
    """
    skip_types = {"audible.last_heard"}
    filtered = [r for r in records if isinstance(r, dict) and r.get("type") not in skip_types]
    return _deduplicate_annotations(filtered)

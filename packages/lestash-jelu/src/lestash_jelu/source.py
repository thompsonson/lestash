"""Jelu source plugin: books you track in Jelu, with your reviews, notes and quotes.

Jelu stays the source of truth for reading history; this is a one-way, read-only copy.
Each book becomes a parent item; reviews, personal notes and quotes are its children.
ISBN and Amazon/ASIN ids are kept in metadata so books can be matched across sources.
"""

import html
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

import httpx
import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_jelu.client import (
    JeluAuthError,
    JeluClient,
    load_credentials,
    save_credentials,
)

console = Console()
logger = get_plugin_logger("jelu")

STATUS_LABELS = {
    "FINISHED": "Finished",
    "CURRENTLY_READING": "Reading",
    "DROPPED": "Dropped",
}


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _strip_html(text: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _names(entries: list[dict[str, Any]] | None) -> list[str]:
    return [e["name"] for e in entries or [] if e.get("name")]


def book_source_id(book_id: str) -> str:
    return f"jelu:book:{book_id}"


def status_label(userbook: dict[str, Any]) -> str:
    """Human-readable reading status of a userbook."""
    label = STATUS_LABELS.get(userbook.get("lastReadingEvent") or "")
    if label:
        return label
    return "To read" if userbook.get("toRead") else "In library"


def userbook_to_item(userbook: dict[str, Any], base_url: str) -> ItemCreate:
    """A Jelu userbook (your copy of a book) as a parent item."""
    book = userbook["book"]
    title = book.get("title") or "Untitled"
    authors = _names(book.get("authors"))
    tags = _names(book.get("tags"))
    series = [s.get("name") for s in book.get("series") or [] if s.get("name")]
    status = status_label(userbook)
    status_date = _parse_date(userbook.get("lastReadingEventDate"))

    lines = [f"{title}" + (f" by {', '.join(authors)}" if authors else "")]
    lines.append(f"Status: {status}" + (f" ({status_date.date()})" if status_date else ""))
    if series:
        lines.append(f"Series: {', '.join(series)}")
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    summary = _strip_html(book.get("summary"))
    if summary:
        lines.append("")
        lines.append(summary)

    metadata: dict[str, Any] = {
        "jelu_book_id": book.get("id"),
        "jelu_userbook_id": userbook.get("id"),
        "isbn13": book.get("isbn13"),
        "isbn10": book.get("isbn10"),
        "asin": book.get("amazonId"),
        "goodreads_id": book.get("goodreadsId"),
        "openlibrary_id": book.get("openlibraryId"),
        "authors": authors,
        "tags": tags,
        "series": series,
        "status": status,
        "status_date": userbook.get("lastReadingEventDate"),
        "to_read": userbook.get("toRead"),
        "owned": userbook.get("owned"),
        "percent_read": userbook.get("percentRead"),
        "language": book.get("language"),
    }
    if book.get("image"):
        metadata["cover_url"] = f"{base_url}/files/{book['image']}"

    return ItemCreate(
        source_type="jelu",
        source_id=book_source_id(book["id"]),
        url=f"{base_url}/books/{userbook['id']}",
        title=title,
        content="\n".join(lines),
        author=", ".join(authors) or None,
        created_at=status_date or _parse_date(userbook.get("creationDate")),
        is_own_content=False,
        metadata={k: v for k, v in metadata.items() if v not in (None, [], "")},
    )


def notes_to_item(userbook: dict[str, Any], base_url: str) -> ItemCreate | None:
    """Your personal notes on a book, as a child item."""
    notes = (userbook.get("personalNotes") or "").strip()
    if not notes:
        return None
    book = userbook["book"]
    return ItemCreate(
        source_type="jelu",
        source_id=f"jelu:notes:{userbook['id']}",
        url=f"{base_url}/books/{userbook['id']}",
        title=f"Notes on {book.get('title') or 'Untitled'}",
        content=notes,
        created_at=_parse_date(userbook.get("modificationDate")),
        is_own_content=True,
        metadata={"type": "notes", "_parent_source_id": book_source_id(book["id"])},
    )


def review_to_item(
    review: dict[str, Any], books: dict[str, dict[str, Any]], base_url: str
) -> ItemCreate:
    """A review you wrote, as a child of its book."""
    userbook = books.get(review.get("book") or "")
    book_title = userbook["book"].get("title") if userbook else None
    rating = review.get("rating")
    stars = f" ({rating / 2:g}/5)" if isinstance(rating, int | float) and rating > 0 else ""
    metadata: dict[str, Any] = {
        "type": "review",
        "rating": rating,
        "visibility": review.get("visibility"),
    }
    if userbook:
        metadata["_parent_source_id"] = book_source_id(review["book"])
    elif review.get("book"):
        # The book isn't in your Jelu library, so no parent item will exist for it
        metadata["jelu_book_id"] = review["book"]
    return ItemCreate(
        source_type="jelu",
        source_id=f"jelu:review:{review['id']}",
        url=f"{base_url}/books/{userbook['id']}" if userbook else None,
        title=f"Review of {book_title or 'a book'}{stars}",
        content=review.get("text") or "",
        created_at=_parse_date(review.get("reviewDate") or review.get("creationDate")),
        is_own_content=True,
        metadata=metadata,
    )


def quote_to_item(
    quote: dict[str, Any], books: dict[str, dict[str, Any]], base_url: str
) -> ItemCreate:
    """A quote you saved, as a child of its book."""
    userbook = books.get(quote.get("book") or "")
    book_title = userbook["book"].get("title") if userbook else None
    metadata: dict[str, Any] = {"type": "quote", "position": quote.get("position")}
    if userbook:
        metadata["_parent_source_id"] = book_source_id(quote["book"])
    elif quote.get("book"):
        # The book isn't in your Jelu library, so no parent item will exist for it
        metadata["jelu_book_id"] = quote["book"]
    return ItemCreate(
        source_type="jelu",
        source_id=f"jelu:quote:{quote['id']}",
        url=f"{base_url}/books/{userbook['id']}" if userbook else None,
        title=f"Quote from {book_title or 'a book'}"
        + (f" ({quote['position']})" if quote.get("position") else ""),
        content=quote.get("text") or "",
        created_at=_parse_date(quote.get("creationDate")),
        is_own_content=False,
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


def sync_items(client: JeluClient) -> Iterator[ItemCreate]:
    """Books first (so children can link to them), then notes, reviews and quotes."""
    # Also proves the session is live: /reviews quietly answers anonymous callers with
    # public reviews only, so an expired session must fail here instead.
    user_id = client.me()["id"]
    userbooks = list(client.userbooks())
    books = {ub["book"]["id"]: ub for ub in userbooks if ub.get("book", {}).get("id")}

    for userbook in userbooks:
        yield userbook_to_item(userbook, client.url)
    for userbook in userbooks:
        notes = notes_to_item(userbook, client.url)
        if notes:
            yield notes
    for review in client.reviews(user_id):
        if review.get("text") and review.get("user", user_id) == user_id:
            yield review_to_item(review, books, client.url)
    for quote in client.quotes(user_id):
        if quote.get("text") and quote.get("user", user_id) == user_id:
            yield quote_to_item(quote, books, client.url)


def _client(creds: dict[str, str]) -> JeluClient:
    return JeluClient(creds["url"], creds["username"], creds["password"], creds.get("token"))


def _remember_token(creds: dict[str, str], client: JeluClient) -> None:
    """Keep the session for next time, so each sync doesn't open a new Jelu session."""
    if client.token and client.token != creds.get("token"):
        save_credentials(creds["url"], creds["username"], creds["password"], client.token)


class JeluSource(SourcePlugin):
    """Jelu reading tracker source plugin."""

    name = "jelu"
    description = "Jelu books, reviews, notes and quotes"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Jelu reading tracker commands.")

        @app.command("auth")
        def auth_cmd(
            url: Annotated[
                str, typer.Option("--url", help="Jelu address, e.g. https://jelu.example:8443")
            ],
            username: Annotated[str, typer.Option("--username", "-u", help="Jelu login")],
        ) -> None:
            """Sign in to Jelu and save the login to ~/.config/lestash/jelu.json (mode 600).

            The password is only ever prompted for: on the command line it would end up in
            shell history and ``ps`` output.
            """
            password = typer.prompt("Jelu password", hide_input=True)
            try:
                with JeluClient(url, username, password) as client:
                    me = client.login()
                    token = client.token
            except (JeluAuthError, httpx.HTTPError) as e:
                console.print(f"[red]Could not sign in to Jelu: {e}[/red]")
                raise typer.Exit(1) from e
            save_credentials(url, username, password, token)
            console.print(f"[green]Saved. Signed in to Jelu as {me.get('login', '?')}.[/green]")

        @app.command("doctor")
        def doctor_cmd() -> None:
            """Check the saved login and count what would be synced."""
            creds = load_credentials()
            if not creds:
                console.print("[red]Not configured. Run: lestash jelu auth --url ... -u ...[/red]")
                raise typer.Exit(1)
            try:
                with _client(creds) as client:
                    me = client.me()
                    userbooks = list(client.userbooks())
                    reviews = list(client.reviews(me["id"]))
                    quotes = list(client.quotes(me["id"]))
                    _remember_token(creds, client)
            except (JeluAuthError, httpx.HTTPError) as e:
                console.print(f"[red]Jelu request failed: {e}[/red]")
                raise typer.Exit(1) from e
            notes = sum(1 for ub in userbooks if (ub.get("personalNotes") or "").strip())
            console.print(f"Connected to {creds['url']} as {me.get('login', '?')}")
            console.print(
                f"{len(userbooks)} books, {len(reviews)} reviews, "
                f"{notes} notes, {len(quotes)} quotes"
            )

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        creds = load_credentials()
        if not creds:
            # Same as the other plugins: an unused plugin shouldn't fail every timer run.
            # A saved login that Jelu rejects does fail the sync (JeluAuthError).
            logger.warning(
                "Jelu is not configured, so nothing was synced. Run "
                "`lestash jelu auth --url <address> -u <login>`, "
                "or `lestash sources disable jelu` to stop this warning."
            )
            return
        with _client(creds) as client:
            yield from sync_items(client)
            _remember_token(creds, client)

    def configure(self) -> dict:
        return {}

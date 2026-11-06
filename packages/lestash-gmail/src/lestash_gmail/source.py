"""Gmail source plugin: threads matching saved Gmail searches (e.g. a mailing list).

Each thread becomes a parent item (its first message); replies are children.
Reads mail through `gws` with read-only access.
"""

import base64
import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_gmail.client import GwsError, GwsGmail, find_gws, load_config, save_config

console = Console()
logger = get_plugin_logger("gmail")

# Each sync re-reads this window so late replies are picked up; the first sync reads everything.
RECENT_WINDOW = "newer_than:30d"

FOOTER_PATTERNS = [
    # Google Groups: from the "-- " separator, or from the sentence if the separator differs
    re.compile(
        r"\n--\s*\nYou received this message because you are subscribed to the Google Groups.*",
        re.S,
    ),
    re.compile(
        r"You received this message because you are subscribed to the Google Groups.*", re.S
    ),
    # Generic mailing-list signature separator followed by list boilerplate
    re.compile(r"\n_{20,}\n.*(unsubscribe|mailing list).*", re.S | re.I),
]
GROUPS_LINK = re.compile(r"https://groups\.google\.com/d/msgid/[^\s>]+")
REPLY_PREFIX = re.compile(r"^\s*((re|fwd?|aw|tr)\s*:\s*)+", re.I)
LIST_TAG = re.compile(r"^\s*\[[^\]]+\]\s*")


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _find_part(part: dict[str, Any], mime: str) -> str | None:
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        return _decode(part["body"]["data"])
    for child in part.get("parts") or []:
        found = _find_part(child, mime)
        if found:
            return found
    return None


def message_text(message: dict[str, Any]) -> str:
    """Plain-text body (HTML stripped if there is no text part), without list footers."""
    payload = message.get("payload", {})
    text = _find_part(payload, "text/plain")
    if text is None:
        raw = _find_part(payload, "text/html") or ""
        raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
        raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw)
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = text.replace("\r\n", "\n")
    for pattern in FOOTER_PATTERNS:
        text = pattern.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _message_date(message: dict[str, Any], headers: dict[str, str]) -> datetime | None:
    if message.get("internalDate"):
        return datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)
    try:
        return parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError):
        return None


def clean_subject(subject: str) -> str:
    """Drop reply prefixes ("Re:", "Fwd:") and the leading list tag like "[UAI]"."""
    text = REPLY_PREFIX.sub("", subject or "")
    text = REPLY_PREFIX.sub("", LIST_TAG.sub("", text, count=1))
    return text.strip() or "(no subject)"


def message_to_item(
    message: dict[str, Any], query: str, *, parent_source_id: str | None = None
) -> ItemCreate:
    headers = _headers(message)
    raw_text = message_text(message)
    groups_link = GROUPS_LINK.search(_find_part(message.get("payload", {}), "text/plain") or "")
    name, address = parseaddr(headers.get("from", ""))
    subject = headers.get("subject", "")
    is_reply = parent_source_id is not None

    metadata: dict[str, Any] = {
        "type": "reply" if is_reply else "thread",
        "query": query,
        "thread_id": message.get("threadId"),
        "message_id": headers.get("message-id"),
        "from_email": address,
        "list_id": headers.get("list-id"),
        "subject": subject,
        "labels": message.get("labelIds", []),
    }
    if groups_link:
        metadata["groups_url"] = groups_link.group(0)
    if parent_source_id:
        metadata["_parent_source_id"] = parent_source_id

    thread_id = message.get("threadId", "")
    return ItemCreate(
        source_type="gmail",
        source_id=f"gmail:message:{message['id']}" if is_reply else f"gmail:thread:{thread_id}",
        url=groups_link.group(0)
        if groups_link
        else f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        title=("Re: " if is_reply else "") + clean_subject(subject),
        content=raw_text or subject,
        author=name or address or None,
        created_at=_message_date(message, headers),
        is_own_content=False,
        metadata={k: v for k, v in metadata.items() if v not in (None, "", [])},
    )


def thread_to_items(thread: dict[str, Any], query: str) -> list[ItemCreate]:
    """First message as the parent, the rest as its children (oldest first)."""
    messages = sorted(thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0)))
    if not messages:
        return []
    parent = message_to_item(messages[0], query)
    children = [message_to_item(m, query, parent_source_id=parent.source_id) for m in messages[1:]]
    return [parent, *children]


def sync_queries(
    gmail: GwsGmail, queries: list[str], full: bool, limit: int | None = None
) -> Iterator[ItemCreate]:
    for query in queries:
        search = query if full else f"({query}) {RECENT_WINDOW}"
        for thread_id in gmail.thread_ids(search, limit=limit):
            try:
                yield from thread_to_items(gmail.thread(thread_id), query)
            except GwsError as e:
                logger.warning(f"Skipping thread {thread_id}: {e}")


class GmailSource(SourcePlugin):
    """Gmail source plugin (via the gws CLI)."""

    name = "gmail"
    description = "Gmail threads from saved searches (via gws)"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Gmail commands (read-only, via the gws CLI).")

        @app.command("add-query")
        def add_query(
            query: Annotated[
                str, typer.Argument(help='Gmail search, e.g. "list:uai-list.googlegroups.com"')
            ],
        ) -> None:
            """Add a Gmail search to sync."""
            config = load_config()
            if query not in config["queries"]:
                config["queries"].append(query)
                save_config(config)
            console.print(f"Queries: {config['queries']}")

        @app.command("remove-query")
        def remove_query(query: Annotated[str, typer.Argument()]) -> None:
            """Stop syncing a Gmail search (items already stashed are kept)."""
            config = load_config()
            config["queries"] = [q for q in config["queries"] if q != query]
            save_config(config)
            console.print(f"Queries: {config['queries']}")

        @app.command("doctor")
        def doctor() -> None:
            """Check gws, the Gmail sign-in and how many threads each search matches."""
            config = load_config()
            gws = find_gws(config.get("gws_path"))
            if not gws:
                console.print(
                    "[red]gws not found. Install it, or set gws_path in gmail.json.[/red]"
                )
                raise typer.Exit(1)
            gmail = GwsGmail(gws)
            try:
                console.print(f"gws: {gws} — signed in as {gmail.profile().get('emailAddress')}")
                for query in config["queries"]:
                    count = sum(1 for _ in gmail.thread_ids(query))
                    console.print(f"  {query}: {count} threads")
            except GwsError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from e
            if not config["queries"]:
                console.print("No queries yet: lestash gmail add-query 'list:...'")

        @app.command("backfill")
        def backfill(
            limit: Annotated[
                int | None, typer.Option(help="Stop after this many threads per query")
            ] = None,
        ) -> None:
            """Import all matching threads (not just the last 30 days)."""
            from lestash.core.config import Config
            from lestash.core.database import (
                get_connection,
                resolve_parent_id,
                resolve_parent_source_ids,
                upsert_item,
            )

            config = load_config()
            gws = find_gws(config.get("gws_path"))
            if not gws:
                console.print("[red]gws not found.[/red]")
                raise typer.Exit(1)
            count = 0
            with get_connection(Config.load()) as conn:
                for item in sync_queries(GwsGmail(gws), config["queries"], full=True, limit=limit):
                    resolve_parent_id(conn, item)
                    upsert_item(conn, item)
                    count += 1
                resolve_parent_source_ids(conn, "gmail")
            console.print(f"[green]Imported {count} messages.[/green]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Recent threads (last 30 days) for each saved search. Use `backfill` for history."""
        settings = load_config()
        if not settings["queries"]:
            logger.info("No Gmail queries configured; run `lestash gmail add-query`")
            return
        gws = find_gws(settings.get("gws_path"))
        if not gws:
            logger.warning("gws not found; skipping Gmail sync")
            return
        yield from sync_queries(GwsGmail(gws), settings["queries"], full=False)

    def configure(self) -> dict:
        return {}

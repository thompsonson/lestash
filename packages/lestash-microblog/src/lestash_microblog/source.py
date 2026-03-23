"""Micro.blog source plugin implementation."""

import json
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

from lestash_microblog.client import (
    delete_token,
    get_token_path,
    load_token,
    save_token,
)

console = Console()
logger = get_plugin_logger("microblog")


def extract_property(entry: dict[str, Any], prop: str) -> Any | None:
    """Extract a property from an h-entry.

    Micropub h-entry properties are wrapped in lists.
    This helper extracts the first value.

    Args:
        entry: The h-entry dict with 'properties'.
        prop: The property name to extract.

    Returns:
        The first value of the property, or None if not found.
    """
    props = entry.get("properties", {})
    values = props.get(prop, [])
    if values and len(values) > 0:
        return values[0]
    return None


def extract_property_list(entry: dict[str, Any], prop: str) -> list[Any]:
    """Extract a list property from an h-entry.

    Args:
        entry: The h-entry dict with 'properties'.
        prop: The property name to extract.

    Returns:
        List of property values.
    """
    props = entry.get("properties", {})
    return props.get(prop, [])


def extract_content(entry: dict[str, Any]) -> str:
    """Extract content from an h-entry.

    Content can be a string or a dict with 'html' and/or 'value' keys.

    Args:
        entry: The h-entry dict.

    Returns:
        The content text.
    """
    content = extract_property(entry, "content")

    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        # Prefer plain text value over HTML
        result = content.get("value", content.get("html", ""))
        return str(result) if result else ""

    return str(content)


def parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string.

    Args:
        dt_str: ISO 8601 datetime string.

    Returns:
        Parsed datetime or None if parsing fails.
    """
    if not dt_str:
        return None

    with suppress(ValueError, AttributeError):
        # Handle various ISO 8601 formats
        dt_str = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(dt_str)

    return None


def post_to_item(entry: dict[str, Any], is_own: bool = True) -> ItemCreate:
    """Convert a Micropub h-entry to ItemCreate.

    Args:
        entry: Micropub h-entry dict.
        is_own: Whether this is the user's own content.

    Returns:
        ItemCreate object for storage.
    """
    # Extract main properties
    content = extract_content(entry)
    url = extract_property(entry, "url")
    name = extract_property(entry, "name")  # Title for longer posts
    published = extract_property(entry, "published")
    uid = extract_property(entry, "uid")

    # Parse datetime
    created_at = parse_datetime(published)

    # Build source_id from URL or UID
    source_id = url or uid

    # Extract author info
    author_data = extract_property(entry, "author")
    author_name: str | None = None
    author_url: str | None = None

    if isinstance(author_data, dict):
        author_name = author_data.get("name")
        author_url = author_data.get("url")
    elif isinstance(author_data, str):
        author_name = author_data

    # Build metadata
    metadata: dict[str, Any] = {
        "uid": uid,
    }

    # Add categories (tags)
    categories = extract_property_list(entry, "category")
    if categories:
        metadata["categories"] = categories

    # Add photos
    photos = extract_property_list(entry, "photo")
    if photos:
        metadata["photos"] = photos

    # Add syndication URLs
    syndication = extract_property_list(entry, "syndication")
    if syndication:
        metadata["syndication"] = syndication

    # Add author info to metadata
    if author_name:
        metadata["author_name"] = author_name
    if author_url:
        metadata["author_url"] = author_url

    # Add in-reply-to if present
    in_reply_to = extract_property_list(entry, "in-reply-to")
    if in_reply_to:
        metadata["in_reply_to"] = in_reply_to

    # Add bookmark-of if present
    bookmark_of = extract_property_list(entry, "bookmark-of")
    if bookmark_of:
        metadata["bookmark_of"] = bookmark_of

    # Add like-of if present
    like_of = extract_property_list(entry, "like-of")
    if like_of:
        metadata["like_of"] = like_of

    return ItemCreate(
        source_type="microblog",
        source_id=source_id,
        url=url,
        title=name,
        content=content,
        author=author_name,
        created_at=created_at,
        is_own_content=is_own,
        metadata=metadata,
    )


def json_feed_item_to_item(
    item: dict[str, Any],
    is_own: bool = False,
    conversation_id: str | None = None,
) -> ItemCreate:
    """Convert a Micro.blog JSON Feed item to ItemCreate.

    JSON Feed items have a different structure from Micropub h-entries.
    This handles the /posts/mentions and /posts/conversation format.

    Args:
        item: JSON Feed item dict.
        is_own: Whether this is the user's own content.
        conversation_id: Optional conversation thread identifier.

    Returns:
        ItemCreate object for storage.
    """
    url = item.get("url")
    content = item.get("content_text") or item.get("content_html", "")
    published = item.get("date_published")
    created_at = parse_datetime(published)

    # Use URL as source_id to align with h-entry dedup
    source_id = url

    # Extract author info
    author_data = item.get("author", {})
    author_name = author_data.get("name") if isinstance(author_data, dict) else None
    author_url = author_data.get("url") if isinstance(author_data, dict) else None

    # Extract _microblog extension
    microblog = item.get("_microblog", {})
    author_microblog = author_data.get("_microblog", {}) if isinstance(author_data, dict) else {}

    # Build metadata
    metadata: dict[str, Any] = {}

    if microblog.get("id"):
        metadata["microblog_id"] = microblog["id"]
    if microblog.get("is_mention"):
        metadata["is_mention"] = True
    if microblog.get("in_reply_to"):
        metadata["in_reply_to"] = [microblog["in_reply_to"]]
    if author_name:
        metadata["author_name"] = author_name
    if author_url:
        metadata["author_url"] = author_url
    if author_microblog.get("username"):
        metadata["author_username"] = author_microblog["username"]
    if conversation_id:
        metadata["conversation_id"] = conversation_id

    return ItemCreate(
        source_type="microblog",
        source_id=source_id,
        url=url,
        title=None,  # JSON Feed microblog posts are short-form
        content=content,
        author=author_name,
        created_at=created_at,
        is_own_content=is_own,
        metadata=metadata if metadata else None,
    )


def _upsert_item(conn, item: ItemCreate) -> int:
    """Insert or update an item in the database.

    Returns:
        1 if a row was affected, 0 otherwise.
    """
    metadata_json = json.dumps(item.metadata) if item.metadata else None
    cursor = conn.execute(
        """
        INSERT INTO items (
            source_type, source_id, url, title, content,
            author, created_at, is_own_content, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            content = excluded.content,
            title = excluded.title,
            author = excluded.author,
            metadata = excluded.metadata
        """,
        (
            item.source_type,
            item.source_id,
            item.url,
            item.title,
            item.content,
            item.author,
            item.created_at,
            item.is_own_content,
            metadata_json,
        ),
    )
    return 1 if cursor.rowcount > 0 else 0


class MicroblogSource(SourcePlugin):
    """Micro.blog source plugin."""

    name = "microblog"
    description = "Micro.blog posts and content"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with Micro.blog commands."""
        app = typer.Typer(help="Micro.blog source commands.")

        @app.command("auth")
        def auth_cmd(
            token: Annotated[
                str | None,
                typer.Option("--token", "-t", help="Micro.blog API token"),
            ] = None,
        ) -> None:
            """Authenticate with Micro.blog.

            Get your API token from https://micro.blog/account/apps

            Saves your token for future use.

            Example:
                lestash microblog auth --token YOUR_TOKEN
            """
            if not token:
                token = typer.prompt("API token (from micro.blog/account/apps)")

            # Verify token works
            try:
                from lestash_microblog.client import create_client

                console.print("[dim]Verifying token...[/dim]")
                with create_client(token) as client:
                    config = client.verify_token()

                # Save token
                save_token(token)

                # Show success
                console.print("[green]✓ Token verified and saved[/green]")
                console.print(f"[dim]Token saved to {get_token_path()}[/dim]")

                # Show destinations if available
                destinations = config.get("destination", [])
                if destinations:
                    console.print(f"\n[bold]Available blogs ({len(destinations)}):[/bold]")
                    for dest in destinations:
                        console.print(
                            f"  - {dest.get('name', 'Unknown')} ({dest.get('uid', 'N/A')})"
                        )

            except Exception as e:
                logger.error(f"Authentication failed: {e}", exc_info=True)
                console.print(f"[red]Authentication failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("logout")
        def logout_cmd() -> None:
            """Remove saved Micro.blog credentials.

            Example:
                lestash microblog logout
            """
            if delete_token():
                console.print("[green]✓ Token removed[/green]")
            else:
                console.print("[yellow]No token found to remove[/yellow]")

        @app.command("sync")
        def sync_cmd(
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Maximum posts per request"),
            ] = 100,
            destination: Annotated[
                str | None,
                typer.Option("--destination", "-d", help="Blog destination UID"),
            ] = None,
            max_posts: Annotated[
                int | None,
                typer.Option("--max", "-m", help="Maximum total posts to sync"),
            ] = None,
            mentions: Annotated[
                bool,
                typer.Option("--mentions/--no-mentions", help="Sync mentions"),
            ] = False,
            conversations: Annotated[
                bool,
                typer.Option(
                    "--conversations/--no-conversations",
                    help="Sync conversation threads for your posts",
                ),
            ] = False,
            sync_all: Annotated[
                bool,
                typer.Option("--all", help="Sync posts, mentions, and conversations"),
            ] = False,
        ) -> None:
            """Sync your Micro.blog posts to the knowledge base.

            Fetches your posts from Micro.blog and stores them locally.
            Use --mentions to also fetch replies from others, --conversations
            to fetch full threads, or --all for everything.

            Example:
                lestash microblog sync
                lestash microblog sync --mentions
                lestash microblog sync --all
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            if sync_all:
                mentions = True
                conversations = True

            # Check for token
            token = load_token()
            if not token:
                console.print("[red]Not authenticated. Run 'lestash microblog auth' first.[/red]")
                raise typer.Exit(1)

            try:
                from lestash_microblog.client import create_client

                config = Config.load()
                items_added = 0

                with create_client(token) as client, get_connection(config) as conn:
                    # Phase 1: Sync own posts
                    console.print("[dim]Syncing posts from Micro.blog...[/dim]")
                    posts = client.get_all_posts(
                        limit=limit,
                        destination=destination,
                        max_posts=max_posts,
                    )
                    console.print(f"[dim]Found {len(posts)} posts[/dim]")

                    current_username = None
                    for post in posts:
                        try:
                            item = post_to_item(post, is_own=True)
                            items_added += _upsert_item(conn, item)
                            # Detect current username from own posts
                            if current_username is None and item.metadata:
                                current_username = item.metadata.get("author_name")
                        except Exception as e:
                            sid = extract_property(post, "url") or extract_property(post, "uid")
                            logger.warning(f"Failed to process post {sid}: {e}")

                    conn.commit()
                    console.print(f"[green]Synced {items_added} posts[/green]")

                    # Phase 2: Sync mentions
                    if mentions:
                        console.print("[dim]Syncing mentions...[/dim]")
                        mention_items = client.get_all_mentions(count=limit)
                        console.print(f"[dim]Found {len(mention_items)} mentions[/dim]")

                        mentions_added = 0
                        for m in mention_items:
                            try:
                                item = json_feed_item_to_item(m, is_own=False)
                                mentions_added += _upsert_item(conn, item)
                            except Exception as e:
                                logger.warning(f"Failed to process mention: {e}")

                        conn.commit()
                        items_added += mentions_added
                        console.print(f"[green]Synced {mentions_added} mentions[/green]")

                    # Phase 3: Sync conversations
                    if conversations:
                        console.print("[dim]Syncing conversations...[/dim]")
                        convos_added = 0
                        seen_urls: set[str] = set()

                        # Fetch conversations for mentions that reply to our posts
                        if not mention_items:
                            mention_items = client.get_all_mentions(count=limit)
                        sources = mention_items
                        for m in sources:
                            microblog = m.get("_microblog", {})
                            reply_to = microblog.get("in_reply_to")
                            post_id = str(microblog.get("id", ""))
                            if not post_id:
                                continue

                            try:
                                thread = client.get_conversation(post_id)
                                for entry in thread:
                                    entry_url = entry.get("url", "")
                                    if entry_url in seen_urls:
                                        continue
                                    seen_urls.add(entry_url)

                                    # Determine is_own
                                    author_mb = (
                                        entry.get("author", {})
                                        .get("_microblog", {})
                                        .get("username", "")
                                    )
                                    entry_is_own = (
                                        current_username is not None
                                        and author_mb == current_username
                                    )

                                    item = json_feed_item_to_item(
                                        entry,
                                        is_own=entry_is_own,
                                        conversation_id=reply_to or entry_url,
                                    )
                                    convos_added += _upsert_item(conn, item)
                            except Exception as e:
                                logger.warning(f"Failed to fetch conversation {post_id}: {e}")

                        conn.commit()
                        items_added += convos_added
                        console.print(f"[green]Synced {convos_added} conversation items[/green]")

                logger.info(f"Sync completed: {items_added} total items")
                console.print(f"[bold green]Total: {items_added} items synced[/bold green]")

            except Exception as e:
                logger.error(f"Sync failed: {e}", exc_info=True)
                console.print(f"[red]Sync failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("status")
        def status_cmd() -> None:
            """Check Micro.blog authentication and account status.

            Displays:
            - Authentication status
            - Available blog destinations
            - Connection status
            """
            console.print("[bold]Micro.blog Status[/bold]\n")

            # Check token
            token = load_token()
            token_path = get_token_path()

            if token:
                # Mask the token for display
                masked = token[:4] + "..." + token[-4:] if len(token) > 8 else "***"
                console.print(f"Token: [green]✓ Found[/green] ({token_path})")
                console.print(f"  Value: {masked}")
            else:
                console.print(f"Token: [red]✗ Not found[/red] ({token_path})")
                console.print("[dim]  Run 'lestash microblog auth' to authenticate[/dim]")
                raise typer.Exit(1)

            # Try to connect
            try:
                from lestash_microblog.client import create_client

                console.print("\n[dim]Checking connection...[/dim]")

                with create_client(token) as client:
                    config = client.verify_token()

                console.print("Connection: [green]✓ Connected[/green]")

                # Show destinations
                destinations = config.get("destination", [])
                if destinations:
                    console.print(f"\n[bold]Available Blogs ({len(destinations)}):[/bold]")

                    table = Table(show_header=True, box=None)
                    table.add_column("Name", style="cyan")
                    table.add_column("UID", style="dim")

                    for dest in destinations:
                        table.add_row(
                            dest.get("name", "Unknown"),
                            dest.get("uid", "N/A"),
                        )

                    console.print(table)

                # Show media endpoint if available
                media_endpoint = config.get("media-endpoint")
                if media_endpoint:
                    console.print(f"\nMedia Endpoint: {media_endpoint}")

            except Exception as e:
                logger.error(f"Status check failed: {e}", exc_info=True)
                console.print("Connection: [red]✗ Failed[/red]")
                console.print(f"[red]{e}[/red]")
                console.print("\n[dim]Run 'lestash microblog auth' to re-authenticate[/dim]")
                raise typer.Exit(1) from None

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync Micro.blog posts, mentions, and conversations.

        Args:
            config: Plugin configuration. Supports keys:
                - destination: Blog destination UID
                - limit: Posts per request (default 100)
                - max_posts: Maximum total posts
                - mentions: bool, sync mentions (default False)
                - conversations: bool, sync conversations (default False)
                - all: bool, sync everything (default False)

        Yields:
            ItemCreate objects for each post/mention/conversation entry.
        """
        from lestash_microblog.client import create_client

        token = load_token()
        if not token:
            logger.warning("No token found for Micro.blog sync")
            return

        do_mentions = config.get("mentions", False) or config.get("all", False)
        do_conversations = config.get("conversations", False) or config.get("all", False)

        try:
            with create_client(token) as client:
                # Phase 1: Own posts
                posts = client.get_all_posts(
                    limit=config.get("limit", 100),
                    destination=config.get("destination"),
                    max_posts=config.get("max_posts"),
                )

                current_username = None
                for post in posts:
                    try:
                        item = post_to_item(post, is_own=True)
                        if current_username is None and item.metadata:
                            current_username = item.metadata.get("author_name")
                        yield item
                    except Exception as e:
                        sid = extract_property(post, "url") or extract_property(post, "uid")
                        logger.warning(f"Failed to process post {sid}: {e}")

                # Phase 2: Mentions
                mention_items = []
                if do_mentions:
                    mention_items = client.get_all_mentions(
                        count=config.get("limit", 100),
                    )
                    for m in mention_items:
                        try:
                            yield json_feed_item_to_item(m, is_own=False)
                        except Exception as e:
                            logger.warning(f"Failed to process mention: {e}")

                # Phase 3: Conversations
                if do_conversations:
                    if not mention_items:
                        mention_items = client.get_all_mentions(
                            count=config.get("limit", 100),
                        )
                    seen_urls: set[str] = set()
                    for m in mention_items:
                        microblog = m.get("_microblog", {})
                        reply_to = microblog.get("in_reply_to")
                        post_id = str(microblog.get("id", ""))
                        if not post_id:
                            continue
                        try:
                            thread = client.get_conversation(post_id)
                            for entry in thread:
                                entry_url = entry.get("url", "")
                                if entry_url in seen_urls:
                                    continue
                                seen_urls.add(entry_url)
                                author_mb = (
                                    entry.get("author", {})
                                    .get("_microblog", {})
                                    .get("username", "")
                                )
                                entry_is_own = (
                                    current_username is not None and author_mb == current_username
                                )
                                yield json_feed_item_to_item(
                                    entry,
                                    is_own=entry_is_own,
                                    conversation_id=reply_to or entry_url,
                                )
                        except Exception as e:
                            logger.warning(f"Failed to fetch conversation {post_id}: {e}")

        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            return

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "destination": None,
            "limit": 100,
        }

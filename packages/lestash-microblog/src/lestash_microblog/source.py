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
        ) -> None:
            """Sync your Micro.blog posts to the knowledge base.

            Fetches all your posts from Micro.blog and stores them locally.
            Uses saved credentials from 'lestash microblog auth'.

            Example:
                lestash microblog sync
                lestash microblog sync --max 50
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            # Check for token
            token = load_token()
            if not token:
                console.print("[red]Not authenticated. Run 'lestash microblog auth' first.[/red]")
                raise typer.Exit(1)

            try:
                from lestash_microblog.client import create_client

                console.print("[dim]Syncing posts from Micro.blog...[/dim]")

                with create_client(token) as client:
                    # Fetch all posts
                    posts = client.get_all_posts(
                        limit=limit,
                        destination=destination,
                        max_posts=max_posts,
                    )
                    console.print(f"[dim]Found {len(posts)} posts[/dim]")

                    # Store in database
                    config = Config.load()
                    items_added = 0

                    with get_connection(config) as conn:
                        for post in posts:
                            try:
                                item = post_to_item(post, is_own=True)
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
                                if cursor.rowcount > 0:
                                    items_added += 1
                            except Exception as e:
                                source_id = extract_property(post, "url") or extract_property(
                                    post, "uid"
                                )
                                logger.warning(f"Failed to process post {source_id}: {e}")
                                continue

                        conn.commit()

                logger.info(f"Sync completed: {items_added} items added")
                console.print(f"[green]Synced {items_added} posts from Micro.blog[/green]")

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
        """Sync Micro.blog posts.

        Args:
            config: Plugin configuration (may contain 'destination', 'limit').

        Yields:
            ItemCreate objects for each post.
        """
        from lestash_microblog.client import create_client

        token = load_token()
        if not token:
            logger.warning("No token found for Micro.blog sync")
            return

        try:
            with create_client(token) as client:
                posts = client.get_all_posts(
                    limit=config.get("limit", 100),
                    destination=config.get("destination"),
                    max_posts=config.get("max_posts"),
                )

                for post in posts:
                    try:
                        yield post_to_item(post, is_own=True)
                    except Exception as e:
                        source_id = extract_property(post, "url") or extract_property(post, "uid")
                        logger.warning(f"Failed to process post {source_id}: {e}")
                        continue

        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            return

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "destination": None,
            "limit": 100,
        }

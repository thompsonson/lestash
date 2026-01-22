"""Bluesky source plugin implementation."""

import json
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_bluesky.client import (
    get_credentials_path,
    get_session_path,
    load_credentials,
    save_credentials,
)

if TYPE_CHECKING:
    from atproto import models

console = Console()
logger = get_plugin_logger("bluesky")


def extract_text_from_facets(text: str, facets: list | None) -> dict[str, list[str]]:
    """Extract mentions, links, and hashtags from facets.

    Args:
        text: Post text
        facets: Facet annotations from AT Protocol

    Returns:
        Dict with 'mentions', 'links', and 'hashtags' lists
    """
    from atproto import models

    result = {"mentions": [], "links": [], "hashtags": []}

    if not facets:
        return result

    for facet in facets:
        for feature in facet.features:
            if isinstance(feature, models.AppBskyRichtextFacet.Mention):
                result["mentions"].append(feature.did)
            elif isinstance(feature, models.AppBskyRichtextFacet.Link):
                result["links"].append(feature.uri)
            elif hasattr(models.AppBskyRichtextFacet, "Tag") and isinstance(
                feature, models.AppBskyRichtextFacet.Tag
            ):
                result["hashtags"].append(feature.tag)

    return result


def post_to_item(post: "models.AppBskyFeedDefs.FeedViewPost", handle: str) -> ItemCreate:
    """Convert Bluesky post to ItemCreate.

    Args:
        post: Feed view post from AT Protocol
        handle: User's handle for determining ownership

    Returns:
        ItemCreate object for storage
    """
    from atproto import models

    record = post.post.record
    author = post.post.author

    # Parse creation time
    created_at = None
    if hasattr(record, "created_at"):
        with suppress(ValueError, AttributeError):
            created_at = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))

    # Extract facets (mentions, links, hashtags)
    facets_data = extract_text_from_facets(
        record.text,
        record.facets if hasattr(record, "facets") else None,
    )

    # Build post URL
    post_id = post.post.uri.split("/")[-1]
    url = f"https://bsky.app/profile/{author.handle}/post/{post_id}"

    # Check if this is own content
    is_own_content = author.handle == handle or author.did == handle

    # Build metadata
    metadata = {
        "cid": post.post.cid,
        "uri": post.post.uri,
        "author_did": author.did,
        "author_handle": author.handle,
        "author_display_name": author.display_name,
        "facets": facets_data,
        "reply_count": post.post.reply_count or 0,
        "repost_count": post.post.repost_count or 0,
        "like_count": post.post.like_count or 0,
    }

    # Add reply information if present
    if hasattr(record, "reply") and record.reply:
        metadata["reply_to"] = {
            "parent": record.reply.parent.uri,
            "root": record.reply.root.uri,
        }

    # Add embed information if present
    if hasattr(record, "embed") and record.embed:
        embed_type = type(record.embed).__name__
        metadata["embed_type"] = embed_type

        # Handle images
        if isinstance(record.embed, models.AppBskyEmbedImages.Main):
            metadata["images"] = [
                {
                    "alt": img.alt,
                    "aspect_ratio": {
                        "width": img.aspect_ratio.width,
                        "height": img.aspect_ratio.height,
                    }
                    if img.aspect_ratio
                    else None,
                }
                for img in record.embed.images
            ]

        # Handle external links
        elif isinstance(record.embed, models.AppBskyEmbedExternal.Main):
            metadata["external"] = {
                "uri": record.embed.external.uri,
                "title": record.embed.external.title,
                "description": record.embed.external.description,
            }

        # Handle record embeds (quotes)
        elif isinstance(record.embed, models.AppBskyEmbedRecord.Main):
            metadata["quoted_post"] = record.embed.record.uri

    # Add language tags if present
    if hasattr(record, "langs") and record.langs:
        metadata["langs"] = record.langs

    return ItemCreate(
        source_type="bluesky",
        source_id=post.post.uri,
        url=url,
        content=record.text,
        author=author.display_name or author.handle,
        created_at=created_at,
        is_own_content=is_own_content,
        metadata=metadata,
    )


class BlueskySource(SourcePlugin):
    """Bluesky source plugin."""

    name = "bluesky"
    description = "Bluesky posts and content"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with Bluesky commands."""
        app = typer.Typer(help="Bluesky source commands.")

        @app.command("auth")
        def auth_cmd(
            handle: Annotated[
                str | None,
                typer.Option("--handle", "-u", help="Bluesky handle (e.g., user.bsky.social)"),
            ] = None,
            password: Annotated[
                str | None,
                typer.Option("--password", "-p", help="Account password", hide_input=True),
            ] = None,
        ) -> None:
            """Authenticate with Bluesky.

            Saves your credentials for future use. If credentials are already saved,
            you can run without arguments to re-authenticate.

            Example:
                lestash bluesky auth --handle user.bsky.social
            """
            # Load existing credentials if not provided
            creds = load_credentials()

            if not handle:
                if creds:
                    handle = creds.get("handle")
                else:
                    handle = typer.prompt("Bluesky handle (e.g., user.bsky.social)")

            if not password:
                if creds and creds.get("handle") == handle:
                    # Reuse saved password
                    password = creds.get("password")
                    console.print("[dim]Using saved password[/dim]")
                else:
                    password = typer.prompt("Password", hide_input=True)

            # Authenticate
            try:
                from lestash_bluesky.client import create_client

                console.print(f"[dim]Authenticating as {handle}...[/dim]")
                client = create_client(handle, password)

                # Save credentials
                save_credentials(handle, password)

                console.print(f"[green]✓ Authenticated as {client.me.handle}[/green]")
                console.print(f"[dim]DID: {client.me.did}[/dim]")
                console.print(f"[dim]Credentials saved to {get_credentials_path()}[/dim]")
                console.print(f"[dim]Session saved to {get_session_path()}[/dim]")

            except Exception as e:
                logger.error(f"Authentication failed: {e}", exc_info=True)
                console.print(f"[red]Authentication failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("sync")
        def sync_cmd(
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Maximum posts per request (max 100)"),
            ] = 100,
        ) -> None:
            """Sync your Bluesky posts to the knowledge base.

            Fetches all your posts from Bluesky and stores them locally.
            Uses saved credentials from 'lestash bluesky auth'.

            Example:
                lestash bluesky sync
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            # Check for credentials
            creds = load_credentials()
            if not creds:
                console.print("[red]Not authenticated. Run 'lestash bluesky auth' first.[/red]")
                raise typer.Exit(1)

            handle = creds["handle"]

            try:
                from lestash_bluesky.client import create_client, get_author_posts

                console.print(f"[dim]Syncing posts for {handle}...[/dim]")
                client = create_client()

                # Fetch all posts
                posts = get_author_posts(client, handle, limit=limit)
                console.print(f"[dim]Found {len(posts)} posts[/dim]")

                # Store in database
                config = Config.load()
                items_added = 0

                with get_connection(config) as conn:
                    for post in posts:
                        try:
                            item = post_to_item(post, handle)
                            metadata_json = json.dumps(item.metadata) if item.metadata else None

                            cursor = conn.execute(
                                """
                                INSERT INTO items (
                                    source_type, source_id, url, title, content,
                                    author, created_at, is_own_content, metadata
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(source_type, source_id) DO UPDATE SET
                                    content = excluded.content,
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
                            logger.warning(f"Failed to process post {post.post.uri}: {e}")
                            continue

                    conn.commit()

                logger.info(f"Sync completed: {items_added} items added")
                console.print(f"[green]Synced {items_added} posts from Bluesky[/green]")

            except Exception as e:
                logger.error(f"Sync failed: {e}", exc_info=True)
                console.print(f"[red]Sync failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("status")
        def status_cmd() -> None:
            """Check Bluesky authentication and account status.

            Displays:
            - Authentication status
            - Account information (handle, DID)
            - Post count
            - Connection status
            """
            console.print("[bold]Bluesky Status[/bold]\n")

            # Check credentials
            creds = load_credentials()
            creds_path = get_credentials_path()

            if creds:
                handle = creds.get("handle")
                console.print(f"Credentials: [green]✓ Found[/green] ({creds_path})")
                console.print(f"  Handle: {handle}")
            else:
                console.print(f"Credentials: [red]✗ Not found[/red] ({creds_path})")
                console.print(
                    "[dim]  Run 'lestash bluesky auth' to authenticate[/dim]"
                )
                raise typer.Exit(1)

            # Try to authenticate
            try:
                from lestash_bluesky.client import create_client

                console.print("\n[dim]Checking connection...[/dim]")
                client = create_client()

                console.print("Connection: [green]✓ Connected[/green]")
                console.print(f"  Handle: {client.me.handle}")
                console.print(f"  DID: {client.me.did}")

                # Get profile info
                profile = client.app.bsky.actor.get_profile({"actor": client.me.handle})

                table = Table(show_header=False, box=None)
                table.add_column("Metric", style="dim")
                table.add_column("Value")

                table.add_row("Display Name", profile.display_name or "-")
                table.add_row("Followers", str(profile.followers_count or 0))
                table.add_row("Following", str(profile.follows_count or 0))
                table.add_row("Posts", str(profile.posts_count or 0))

                console.print()
                console.print(table)

            except Exception as e:
                logger.error(f"Status check failed: {e}", exc_info=True)
                console.print("Connection: [red]✗ Failed[/red]")
                console.print(f"[red]{e}[/red]")
                console.print("\n[dim]Run 'lestash bluesky auth' to re-authenticate[/dim]")
                raise typer.Exit(1) from None

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync Bluesky posts.

        Args:
            config: Plugin configuration (should contain 'handle')

        Yields:
            ItemCreate objects for each post
        """
        from lestash_bluesky.client import create_client, get_author_posts

        handle = config.get("handle")
        if not handle:
            logger.warning("No handle configured for Bluesky sync")
            return

        try:
            client = create_client()
            posts = get_author_posts(client, handle, limit=config.get("limit", 100))

            for post in posts:
                try:
                    yield post_to_item(post, handle)
                except Exception as e:
                    logger.warning(f"Failed to process post {post.post.uri}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            return

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "handle": "your.handle.bsky.social",
            "limit": 100,
        }

"""YouTube source plugin implementation."""

import json
import re
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

from lestash_youtube.client import (
    check_client_secrets,
    create_youtube_client,
    get_channel_info,
    get_client_secrets_path,
    get_credentials_path,
    get_liked_videos,
    get_subscriptions,
    get_watch_history,
    load_credentials,
    run_oauth_flow,
)

console = Console()
logger = get_plugin_logger("youtube")


def parse_iso8601_duration(duration: str | None) -> int | None:
    """Parse ISO 8601 duration string to seconds.

    Args:
        duration: Duration string like "PT1H2M3S" or "PT5M30S"

    Returns:
        Duration in seconds, or None if parsing fails
    """
    if not duration:
        return None

    # Pattern for ISO 8601 duration: PT[nH][nM][nS]
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, duration)

    if not match:
        return None

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)

    return hours * 3600 + minutes * 60 + seconds


def video_to_item(video: dict[str, Any], source_subtype: str = "liked") -> ItemCreate:
    """Convert YouTube video data to ItemCreate.

    Args:
        video: Video data dictionary from API
        source_subtype: Type of video source ("liked", "history", "uploaded")

    Returns:
        ItemCreate object for storage
    """
    # For liked videos, use liked_at as the item's created_at
    # For history, use watched_at
    # Fall back to published_at if neither is available
    created_at = None
    action_timestamp = video.get("liked_at") or video.get("watched_at")

    if action_timestamp:
        with suppress(ValueError, AttributeError):
            created_at = datetime.fromisoformat(action_timestamp.replace("Z", "+00:00"))

    # Fall back to published_at if no action timestamp
    if created_at is None and video.get("published_at"):
        with suppress(ValueError, AttributeError):
            created_at = datetime.fromisoformat(video["published_at"].replace("Z", "+00:00"))

    # Build YouTube URL
    video_id = video.get("id")
    url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None

    # Parse duration
    duration_seconds = parse_iso8601_duration(video.get("duration"))

    # Get best thumbnail URL
    thumbnails = video.get("thumbnails", {})
    thumbnail_url = None
    for quality in ["maxres", "high", "medium", "default"]:
        if quality in thumbnails:
            thumbnail_url = thumbnails[quality].get("url")
            break

    # Build metadata
    metadata = {
        "video_id": video_id,
        "channel_id": video.get("channel_id"),
        "channel_title": video.get("channel_title"),
        "source_subtype": source_subtype,
        "duration_seconds": duration_seconds,
        "duration_iso": video.get("duration"),
        "definition": video.get("definition"),
        "thumbnail_url": thumbnail_url,
        "tags": video.get("tags", []),
        "category_id": video.get("category_id"),
    }

    # Store original video publish date in metadata
    if video.get("published_at"):
        metadata["published_at"] = video["published_at"]

    # Add statistics if available
    if video.get("view_count"):
        metadata["view_count"] = int(video["view_count"])
    if video.get("like_count"):
        metadata["like_count"] = int(video["like_count"])
    if video.get("comment_count"):
        metadata["comment_count"] = int(video["comment_count"])

    # Store action timestamps in metadata
    if video.get("liked_at"):
        metadata["liked_at"] = video["liked_at"]
    if video.get("watched_at"):
        metadata["watched_at"] = video["watched_at"]

    return ItemCreate(
        source_type="youtube",
        source_id=f"{source_subtype}:{video_id}",
        url=url,
        title=video.get("title"),
        content=video.get("description") or "",
        author=video.get("channel_title"),
        created_at=created_at,
        is_own_content=False,  # Liked/watched videos are not own content
        metadata=metadata,
    )


def subscription_to_item(subscription: dict[str, Any]) -> ItemCreate:
    """Convert YouTube subscription to ItemCreate.

    Args:
        subscription: Subscription data dictionary from API

    Returns:
        ItemCreate object for storage
    """
    created_at = None
    if subscription.get("published_at"):
        with suppress(ValueError, AttributeError):
            created_at = datetime.fromisoformat(subscription["published_at"].replace("Z", "+00:00"))

    channel_id = subscription.get("channel_id")
    url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else None

    thumbnails = subscription.get("thumbnails", {})
    thumbnail_url = None
    for quality in ["high", "medium", "default"]:
        if quality in thumbnails:
            thumbnail_url = thumbnails[quality].get("url")
            break

    metadata = {
        "subscription_id": subscription.get("id"),
        "channel_id": channel_id,
        "source_subtype": "subscription",
        "thumbnail_url": thumbnail_url,
    }

    return ItemCreate(
        source_type="youtube",
        source_id=f"subscription:{channel_id}",
        url=url,
        title=subscription.get("title"),
        content=subscription.get("description") or "",
        author=subscription.get("title"),
        created_at=created_at,
        is_own_content=False,
        metadata=metadata,
    )


class YouTubeSource(SourcePlugin):
    """YouTube source plugin."""

    name = "youtube"
    description = "YouTube liked videos, watch history, and subscriptions"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with YouTube commands."""
        app = typer.Typer(help="YouTube source commands.")

        @app.command("auth")
        def auth_cmd() -> None:
            """Authenticate with YouTube using OAuth 2.0.

            Before running this command, you need to set up OAuth credentials:

            1. Go to https://console.cloud.google.com/apis/credentials
            2. Create a project (or select existing one)
            3. Enable the YouTube Data API v3
            4. Create OAuth 2.0 Client ID (Application type: Desktop app)
            5. Download the JSON file
            6. Save it as: ~/.config/lestash/youtube_client_secrets.json

            Then run: lestash youtube auth
            """
            secrets_path = get_client_secrets_path()

            if not check_client_secrets():
                console.print("[red]OAuth client secrets not found.[/red]\n")
                console.print("To authenticate with YouTube, you need OAuth credentials:\n")
                console.print(
                    "1. Go to [link]https://console.cloud.google.com/apis/credentials[/link]"
                )
                console.print("2. Create a project (or select existing one)")
                console.print("3. Enable the [bold]YouTube Data API v3[/bold]")
                console.print("4. Create [bold]OAuth 2.0 Client ID[/bold] (Desktop application)")
                console.print("5. Download the JSON file")
                console.print(f"6. Save it as: [cyan]{secrets_path}[/cyan]\n")
                console.print("Then run: [green]lestash youtube auth[/green]")
                raise typer.Exit(1)

            try:
                console.print("[dim]Starting OAuth flow...[/dim]")
                console.print("[dim]A browser window will open for authentication.[/dim]\n")

                run_oauth_flow()

                console.print("[green]Authentication successful![/green]")
                console.print(f"[dim]Credentials saved to {get_credentials_path()}[/dim]")

                # Show account info
                youtube = create_youtube_client()
                channel = get_channel_info(youtube)

                if channel:
                    console.print(f"\n[bold]Connected as:[/bold] {channel['title']}")
                    if channel.get("custom_url"):
                        url = channel["custom_url"]
                        console.print(f"[dim]Channel URL: youtube.com/{url}[/dim]")

            except FileNotFoundError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from None
            except Exception as e:
                logger.error(f"Authentication failed: {e}", exc_info=True)
                console.print(f"[red]Authentication failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("status")
        def status_cmd() -> None:
            """Check YouTube authentication and account status.

            Displays:
            - Authentication status
            - Channel information
            - API access status
            """
            console.print("[bold]YouTube Status[/bold]\n")

            # Check client secrets
            secrets_path = get_client_secrets_path()
            if check_client_secrets():
                console.print(f"Client Secrets: [green]Found[/green] ({secrets_path})")
            else:
                console.print(f"Client Secrets: [red]Not found[/red] ({secrets_path})")
                console.print("[dim]  Run 'lestash youtube auth' for setup instructions[/dim]")
                raise typer.Exit(1)

            # Check credentials
            creds_path = get_credentials_path()
            creds = load_credentials()
            if creds:
                console.print(f"Credentials: [green]Found[/green] ({creds_path})")
            else:
                console.print(f"Credentials: [red]Not found[/red] ({creds_path})")
                console.print("[dim]  Run 'lestash youtube auth' to authenticate[/dim]")
                raise typer.Exit(1)

            # Try to connect
            try:
                console.print("\n[dim]Checking API connection...[/dim]")
                youtube = create_youtube_client()
                channel = get_channel_info(youtube)

                if channel:
                    console.print("Connection: [green]Connected[/green]\n")

                    table = Table(show_header=False, box=None)
                    table.add_column("Field", style="dim")
                    table.add_column("Value")

                    table.add_row("Channel", channel["title"])
                    if channel.get("custom_url"):
                        table.add_row("URL", f"youtube.com/{channel['custom_url']}")
                    table.add_row("Subscribers", str(channel.get("subscriber_count", "N/A")))
                    table.add_row("Videos", str(channel.get("video_count", "N/A")))
                    table.add_row("Total Views", str(channel.get("view_count", "N/A")))

                    console.print(table)
                else:
                    console.print("Connection: [yellow]Connected but no channel found[/yellow]")

            except Exception as e:
                logger.error(f"Status check failed: {e}", exc_info=True)
                console.print("Connection: [red]Failed[/red]")
                console.print(f"[red]{e}[/red]")
                console.print("\n[dim]Run 'lestash youtube auth' to re-authenticate[/dim]")
                raise typer.Exit(1) from None

        @app.command("sync")
        def sync_cmd(
            likes: Annotated[
                bool,
                typer.Option("--likes/--no-likes", help="Sync liked videos"),
            ] = True,
            history: Annotated[
                bool,
                typer.Option("--history/--no-history", help="Attempt to sync watch history"),
            ] = True,
            subscriptions: Annotated[
                bool,
                typer.Option("--subscriptions/--no-subscriptions", "-s", help="Sync subscriptions"),
            ] = False,
        ) -> None:
            """Sync YouTube data to the knowledge base.

            By default, syncs liked videos and attempts watch history.
            Watch history may be empty due to YouTube API restrictions.

            Examples:
                lestash youtube sync                    # Sync likes + try history
                lestash youtube sync --no-history       # Only liked videos
                lestash youtube sync --subscriptions    # Include subscriptions
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            # Check credentials
            creds = load_credentials()
            if not creds:
                console.print("[red]Not authenticated. Run 'lestash youtube auth' first.[/red]")
                raise typer.Exit(1)

            try:
                youtube = create_youtube_client()
                config = Config.load()
                total_added = 0

                with get_connection(config) as conn:
                    # Sync liked videos
                    if likes:
                        console.print("[dim]Fetching liked videos...[/dim]")
                        liked_videos = get_liked_videos(youtube)
                        console.print(f"[dim]Found {len(liked_videos)} liked videos[/dim]")

                        for video in liked_videos:
                            try:
                                item = video_to_item(video, "liked")
                                total_added += _store_item(conn, item)
                            except Exception as e:
                                logger.warning(f"Failed to process video {video.get('id')}: {e}")

                        conn.commit()
                        console.print(f"[green]Synced {len(liked_videos)} liked videos[/green]")

                    # Attempt watch history
                    if history:
                        console.print("[dim]Attempting to fetch watch history...[/dim]")
                        history_videos = get_watch_history(youtube)

                        if history_videos:
                            console.print(f"[dim]Found {len(history_videos)} history items[/dim]")

                            for video in history_videos:
                                try:
                                    item = video_to_item(video, "history")
                                    total_added += _store_item(conn, item)
                                except Exception as e:
                                    vid = video.get("id")
                                    logger.warning(f"Failed to process video {vid}: {e}")

                            conn.commit()
                            count = len(history_videos)
                            console.print(f"[green]Synced {count} history items[/green]")
                        else:
                            console.print(
                                "[yellow]Watch history is empty or restricted.[/yellow]\n"
                                "[dim]YouTube restricts API access to watch history.\n"
                                "Consider using Google Takeout for full history:\n"
                                "https://takeout.google.com (select YouTube > History)[/dim]"
                            )

                    # Sync subscriptions
                    if subscriptions:
                        console.print("[dim]Fetching subscriptions...[/dim]")
                        subs = get_subscriptions(youtube)
                        console.print(f"[dim]Found {len(subs)} subscriptions[/dim]")

                        for sub in subs:
                            try:
                                item = subscription_to_item(sub)
                                total_added += _store_item(conn, item)
                            except Exception as e:
                                logger.warning(f"Failed to process subscription: {e}")

                        conn.commit()
                        console.print(f"[green]Synced {len(subs)} subscriptions[/green]")

                logger.info(f"Sync completed: {total_added} items added/updated")
                console.print(f"\n[bold green]Total: {total_added} items synced[/bold green]")

            except Exception as e:
                logger.error(f"Sync failed: {e}", exc_info=True)
                console.print(f"[red]Sync failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("likes")
        def likes_cmd(
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Maximum videos to show"),
            ] = 20,
        ) -> None:
            """List your liked videos without syncing.

            Useful for previewing what will be synced.
            """
            creds = load_credentials()
            if not creds:
                console.print("[red]Not authenticated. Run 'lestash youtube auth' first.[/red]")
                raise typer.Exit(1)

            try:
                youtube = create_youtube_client()
                console.print("[dim]Fetching liked videos...[/dim]\n")

                videos = get_liked_videos(youtube)

                table = Table(title=f"Liked Videos ({len(videos)} total)")
                table.add_column("Title", max_width=50)
                table.add_column("Channel", max_width=25)
                table.add_column("Duration")
                table.add_column("Published")

                for video in videos[:limit]:
                    duration = parse_iso8601_duration(video.get("duration"))
                    duration_str = ""
                    if duration:
                        mins, secs = divmod(duration, 60)
                        hours, mins = divmod(mins, 60)
                        if hours:
                            duration_str = f"{hours}:{mins:02d}:{secs:02d}"
                        else:
                            duration_str = f"{mins}:{secs:02d}"

                    published = video.get("published_at", "")[:10]

                    table.add_row(
                        video.get("title", "")[:50],
                        video.get("channel_title", "")[:25],
                        duration_str,
                        published,
                    )

                console.print(table)

                if len(videos) > limit:
                    console.print(f"\n[dim]Showing {limit} of {len(videos)} videos[/dim]")

            except Exception as e:
                logger.error(f"Failed to fetch likes: {e}", exc_info=True)
                console.print(f"[red]Failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("history")
        def history_cmd(
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Maximum videos to show"),
            ] = 20,
        ) -> None:
            """Attempt to list watch history.

            Note: YouTube API access to watch history is restricted.
            This may return empty results. Use Google Takeout for full history.
            """
            creds = load_credentials()
            if not creds:
                console.print("[red]Not authenticated. Run 'lestash youtube auth' first.[/red]")
                raise typer.Exit(1)

            try:
                youtube = create_youtube_client()
                console.print("[dim]Attempting to fetch watch history...[/dim]\n")

                videos = get_watch_history(youtube)

                if not videos:
                    console.print("[yellow]Watch history is empty or restricted.[/yellow]\n")
                    console.print("YouTube restricts API access to watch history.")
                    console.print("For full history, use Google Takeout:")
                    console.print("  1. Go to [link]https://takeout.google.com[/link]")
                    console.print(
                        "  2. Deselect all, then select [bold]YouTube and YouTube Music[/bold]"
                    )
                    console.print(
                        "  3. Click 'Multiple formats' and set History to [bold]JSON[/bold]"
                    )
                    console.print("  4. Export and download")
                    console.print("\n[dim]Takeout import coming in future update.[/dim]")
                    return

                table = Table(title=f"Watch History ({len(videos)} items)")
                table.add_column("Title", max_width=50)
                table.add_column("Channel", max_width=25)
                table.add_column("Watched")

                for video in videos[:limit]:
                    watched = video.get("watched_at", "")[:10]
                    table.add_row(
                        video.get("title", "")[:50],
                        video.get("channel_title", "")[:25],
                        watched,
                    )

                console.print(table)

            except Exception as e:
                logger.error(f"Failed to fetch history: {e}", exc_info=True)
                console.print(f"[red]Failed: {e}[/red]")
                raise typer.Exit(1) from None

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync YouTube data.

        Args:
            config: Plugin configuration

        Yields:
            ItemCreate objects for each video/subscription
        """
        try:
            youtube = create_youtube_client()

            # Sync liked videos
            if config.get("sync_likes", True):
                for video in get_liked_videos(youtube):
                    try:
                        yield video_to_item(video, "liked")
                    except Exception as e:
                        logger.warning(f"Failed to process video {video.get('id')}: {e}")

            # Attempt watch history
            if config.get("sync_history", True):
                for video in get_watch_history(youtube):
                    try:
                        yield video_to_item(video, "history")
                    except Exception as e:
                        logger.warning(f"Failed to process video {video.get('id')}: {e}")

            # Sync subscriptions
            if config.get("sync_subscriptions", False):
                for sub in get_subscriptions(youtube):
                    try:
                        yield subscription_to_item(sub)
                    except Exception as e:
                        logger.warning(f"Failed to process subscription: {e}")

        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            return

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "sync_likes": True,
            "sync_history": True,
            "sync_subscriptions": False,
        }


def _store_item(conn, item: ItemCreate) -> int:
    """Store an item in the database.

    Args:
        conn: Database connection
        item: Item to store

    Returns:
        1 if item was added/updated, 0 otherwise
    """
    from lestash.core.database import upsert_item

    metadata_json = json.dumps(item.metadata) if item.metadata else None

    return upsert_item(
        conn,
        source_type=item.source_type,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        content=item.content,
        author=item.author,
        created_at=item.created_at,
        is_own_content=item.is_own_content,
        metadata=metadata_json,
    )

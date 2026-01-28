"""LinkedIn source plugin implementation."""

import hashlib
import json
import re
import shutil
import webbrowser
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.prompt import Confirm, Prompt

from lestash_linkedin.api import (
    SNAPSHOT_DOMAINS,
    LinkedInAPI,
    authorize,
    get_credentials_path,
    get_token_path,
    load_credentials,
    load_token,
    save_credentials,
)
from lestash_linkedin.importer import import_from_zip

console = Console()
logger = get_plugin_logger("linkedin")


def parse_linkedin_date(date_str: str) -> datetime | None:
    """Parse LinkedIn API date formats."""
    if not date_str:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return None


def snapshot_to_items(domain: str, data: list[dict]) -> Iterator[ItemCreate]:
    """Convert snapshot data to ItemCreate objects."""
    for record in data:
        if domain == "MEMBER_SHARE_INFO":
            # Posts/shares
            content = record.get("ShareCommentary") or record.get("Commentary") or ""
            if not content.strip():
                continue

            yield ItemCreate(
                source_type="linkedin",
                source_id=record.get("ShareLink") or f"post-{hash(content)}",
                url=record.get("ShareLink"),
                content=content.strip(),
                created_at=parse_linkedin_date(record.get("Date", "")),
                is_own_content=True,
                metadata={
                    "domain": domain,
                    "visibility": record.get("Visibility"),
                    "media_url": record.get("MediaUrl"),
                    "raw": record,
                },
            )

        elif domain == "ALL_COMMENTS":
            content = record.get("Message") or record.get("Comment") or ""
            if not content.strip():
                continue

            yield ItemCreate(
                source_type="linkedin",
                source_id=record.get("Link") or f"comment-{hash(content)}",
                url=record.get("Link"),
                content=content.strip(),
                created_at=parse_linkedin_date(record.get("Date", "")),
                is_own_content=True,
                metadata={"domain": domain, "raw": record},
            )

        elif domain == "ALL_LIKES":
            # Reactions - store the target post info
            target_url = record.get("Link") or record.get("TargetUrl")
            reaction_type = record.get("Type") or record.get("ReactionType") or "Like"

            yield ItemCreate(
                source_type="linkedin",
                source_id=f"reaction-{target_url or hash(str(record))}",
                url=target_url,
                content=f"Reacted with {reaction_type}",
                created_at=parse_linkedin_date(record.get("Date", "")),
                is_own_content=True,
                metadata={"domain": domain, "reaction_type": reaction_type, "raw": record},
            )

        elif domain == "ARTICLES":
            title = record.get("Title") or ""
            content = record.get("Content") or record.get("Body") or ""

            if not title and not content:
                continue

            yield ItemCreate(
                source_type="linkedin",
                source_id=record.get("Link") or f"article-{hash(title + content)}",
                url=record.get("Link"),
                title=title,
                content=content.strip() if content else title,
                created_at=parse_linkedin_date(record.get("Date", "")),
                is_own_content=True,
                metadata={"domain": domain, "raw": record},
            )

        elif domain == "INSTANT_REPOSTS":
            yield ItemCreate(
                source_type="linkedin",
                source_id=record.get("Link") or f"repost-{hash(str(record))}",
                url=record.get("Link"),
                content="Reposted",
                created_at=parse_linkedin_date(record.get("Date", "")),
                is_own_content=True,
                metadata={"domain": domain, "raw": record},
            )


def changelog_to_items(events: list[dict]) -> Iterator[ItemCreate]:
    """Convert changelog events to ItemCreate objects.

    Uses schema-validated extractors for type-safe content extraction.
    Changelog events track activity after consent (posts, comments, reactions, etc.).
    """
    from lestash_linkedin.extractors.changelog import extract_changelog_item

    for event in events:
        yield extract_changelog_item(event)


class LinkedInSource(SourcePlugin):
    """LinkedIn source plugin."""

    name = "linkedin"
    description = "LinkedIn posts and content"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with LinkedIn commands."""
        app = typer.Typer(help="LinkedIn source commands.")

        @app.command("import")
        def import_export(
            zip_path: Annotated[Path, typer.Argument(help="Path to LinkedIn export ZIP file")],
        ) -> None:
            """Import posts from LinkedIn data export ZIP."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            if not zip_path.exists():
                console.print(f"[red]File not found: {zip_path}[/red]")
                raise typer.Exit(1)

            logger.info(f"Importing from ZIP: {zip_path}")
            config = Config.load()
            items_added = 0

            with get_connection(config) as conn:
                for item in import_from_zip(zip_path):
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

                conn.commit()

            logger.info(f"Import completed: {items_added} items added")
            console.print(f"[green]Imported {items_added} posts from LinkedIn export[/green]")

        @app.command("auth")
        def auth_cmd(
            client_id: Annotated[
                str | None,
                typer.Option("--client-id", help="LinkedIn App Client ID"),
            ] = None,
            client_secret: Annotated[
                str | None,
                typer.Option("--client-secret", help="LinkedIn App Client Secret"),
            ] = None,
            mode: Annotated[
                str,
                typer.Option(
                    "--mode",
                    "-m",
                    help="API mode: 'self-serve' (personal) or '3rd-party' (apps)",
                ),
            ] = "3rd-party",
        ) -> None:
            """Authenticate with LinkedIn API (EU/EEA members only).

            Two modes are available:

            SELF-SERVE MODE (personal data mining):
              - Use LinkedIn's default company page during app creation
              - Request "Member Data Portability API (Member)" product
              - Instant access after accepting terms
              - Run: lestash linkedin auth --mode self-serve --client-id ID --client-secret SECRET

            3RD-PARTY MODE (building apps for others):
              - Use your own verified company page
              - Request "Member Data Portability API (3rd Party)" product
              - Requires manual review
              - Run: lestash linkedin auth --client-id ID --client-secret SECRET

            After first use, credentials are stored and you can just run:
                lestash linkedin auth
            """
            # Validate mode
            if mode not in ("self-serve", "3rd-party"):
                console.print(f"[red]Invalid mode: {mode}. Use 'self-serve' or '3rd-party'.[/red]")
                raise typer.Exit(1)

            # Load or get credentials
            creds = load_credentials()

            if client_id and client_secret:
                save_credentials(client_id, client_secret, mode)
                creds = {"client_id": client_id, "client_secret": client_secret, "mode": mode}
            elif not creds:
                console.print("[red]No credentials found.[/red]")
                console.print(
                    "Run with --client-id and --client-secret, or create "
                    "~/.config/lestash/linkedin_credentials.json"
                )
                raise typer.Exit(1)
            else:
                # Use mode from stored credentials if not explicitly provided
                mode = creds.get("mode", "3rd-party")

            console.print(f"[dim]Using {mode} mode[/dim]")

            # Run OAuth flow
            try:
                authorize(creds["client_id"], creds["client_secret"], mode)
            except Exception as e:
                logger.error(f"Authorization failed: {e}", exc_info=True)
                console.print(f"[red]Authorization failed: {e}[/red]")
                raise typer.Exit(1) from None

        @app.command("fetch")
        def fetch_cmd(
            domain: Annotated[
                str | None,
                typer.Option(
                    "--domain",
                    "-d",
                    help="Specific domain to fetch (MEMBER_SHARE_INFO, ALL_COMMENTS, etc.)",
                ),
            ] = None,
            all_domains: Annotated[
                bool,
                typer.Option("--all", help="Fetch all content domains from Snapshot API"),
            ] = False,
            changelog: Annotated[
                bool,
                typer.Option(
                    "--changelog",
                    help="Fetch from Changelog API (activity after consent, past 28 days)",
                ),
            ] = False,
            no_pause: Annotated[
                bool,
                typer.Option(
                    "--no-pause", help="Don't pause on rate limits (429), fail immediately"
                ),
            ] = False,
        ) -> None:
            """Fetch your LinkedIn data via the DMA Portability API.

            Requires prior authentication with 'lestash linkedin auth'.

            Two APIs are available:

            SNAPSHOT API (default):
              Fetches historical data. Use --domain or --all to specify domains:
              - MEMBER_SHARE_INFO: Your posts
              - ALL_COMMENTS: Your comments
              - ALL_LIKES: Your reactions
              - ARTICLES: Your articles
              - INSTANT_REPOSTS: Your reposts

            CHANGELOG API (--changelog):
              Fetches activity tracked after you consented (past 28 days).
              Includes posts, comments, reactions, and other interactions.
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            token = load_token()
            if not token:
                console.print("[red]Not authenticated. Run 'lestash linkedin auth' first.[/red]")
                raise typer.Exit(1)

            access_token = token.get("access_token")
            if not access_token:
                console.print("[red]Invalid token. Run 'lestash linkedin auth' again.[/red]")
                raise typer.Exit(1)

            config = Config.load()
            total_items = 0

            with LinkedInAPI(access_token, pause_for_rate_limiting=not no_pause) as api:
                if changelog:
                    # Fetch from Changelog API
                    logger.info("Starting fetch from Changelog API")
                    console.print("[dim]Fetching from Changelog API...[/dim]")

                    try:
                        events = api.get_all_changelog()
                        console.print(f"[dim]  Found {len(events)} changelog events[/dim]")

                        with get_connection(config) as conn:
                            for item in changelog_to_items(events):
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
                                    total_items += 1
                            conn.commit()

                    except Exception as e:
                        logger.error(f"Error fetching changelog: {e}", exc_info=True)
                        console.print(f"[red]Error fetching changelog: {e}[/red]")
                        raise typer.Exit(1) from None

                else:
                    # Fetch from Snapshot API
                    if domain:
                        domains = [domain.upper()]
                    elif all_domains:
                        domains = SNAPSHOT_DOMAINS
                    else:
                        # Default to profile
                        domains = ["PROFILE"]

                    logger.info(f"Starting fetch for domains: {domains}")

                    for dom in domains:
                        console.print(f"[dim]Fetching {dom}...[/dim]")

                        try:
                            data = api.get_all_snapshot_data(dom)
                            console.print(f"[dim]  Found {len(data)} records[/dim]")

                            with get_connection(config) as conn:
                                for item in snapshot_to_items(dom, data):
                                    metadata_json = (
                                        json.dumps(item.metadata) if item.metadata else None
                                    )
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
                                        total_items += 1

                                conn.commit()

                        except Exception as e:
                            logger.error(f"Error fetching {dom}: {e}", exc_info=True)
                            console.print(f"[yellow]  Error fetching {dom}: {e}[/yellow]")

            logger.info(f"Fetch completed: {total_items} items stored")
            console.print(f"[green]Fetched {total_items} items from LinkedIn[/green]")

        @app.command("doctor")
        def doctor_cmd(
            no_pause: Annotated[
                bool,
                typer.Option(
                    "--no-pause", help="Don't pause on rate limits (429), fail immediately"
                ),
            ] = False,
        ) -> None:
            """Check LinkedIn API configuration and endpoint connectivity."""
            console.print("[bold]Checking LinkedIn API configuration...[/bold]\n")

            # Check credentials
            creds = load_credentials()
            creds_path = get_credentials_path()
            if creds:
                mode = creds.get("mode", "unknown")
                console.print(f"Credentials: [green]✓ Found[/green] ({creds_path})")
                console.print(f"  Mode: {mode}")
            else:
                console.print(f"Credentials: [red]✗ Not found[/red] ({creds_path})")
                console.print(
                    "[dim]  Run 'lestash linkedin auth --client-id ID "
                    "--client-secret SECRET' to configure[/dim]"
                )
                raise typer.Exit(1)

            # Check token
            token = load_token()
            token_path = get_token_path()
            if token and token.get("access_token"):
                console.print(f"Token: [green]✓ Found[/green] ({token_path})")
            else:
                console.print(f"Token: [red]✗ Not found or invalid[/red] ({token_path})")
                console.print("[dim]  Run 'lestash linkedin auth' to authenticate[/dim]")
                raise typer.Exit(1)

            access_token = token["access_token"]

            with LinkedInAPI(access_token, pause_for_rate_limiting=not no_pause) as api:
                # Test Snapshot API - single call to check accessibility
                console.print("\n[bold]Snapshot API (historical data):[/bold]")
                try:
                    result = api.get_snapshot(domain=None, start=0, count=1)
                    paging = result.get("paging", {})
                    total = paging.get("total", 0)
                    elements = result.get("elements", [])

                    if total > 0:
                        sample_domain = elements[0].get("snapshotDomain") if elements else None
                        console.print(
                            f"  [green]✓ Accessible[/green] ({total} data pages available)"
                        )
                        if sample_domain:
                            console.print(f"  Sample domain: {sample_domain}")
                        console.print(
                            "  [dim]Use 'fetch --domain DOMAIN' to fetch specific domains[/dim]"
                        )
                        console.print(
                            "  [dim]Common domains: PROFILE, ARTICLES, CONNECTIONS, INBOX[/dim]"
                        )
                    else:
                        console.print("  [yellow]No snapshot data available[/yellow]")

                except httpx.HTTPStatusError as e:
                    console.print(f"  [red]✗ Error: {e.response.status_code}[/red]")
                except Exception as e:
                    console.print(f"  [red]✗ Error: {e}[/red]")

                # Test Changelog API
                console.print(
                    "\n[bold]Changelog API (activity after consent, past 28 days):[/bold]"
                )
                console.print(
                    "  [dim]Use --changelog to fetch posts, comments, likes after consent[/dim]"
                )
                try:
                    result = api.get_changelog(count=10)
                    elements = result.get("elements", [])
                    if elements:
                        # Count event types
                        event_types: dict[str, int] = {}
                        for elem in elements:
                            resource = elem.get("resourceName", "unknown")
                            event_types[resource] = event_types.get(resource, 0) + 1

                        console.print(f"  [green]✓ Accessible[/green] ({len(elements)}+ events)")
                        console.print("  Recent activity types:")
                        for resource, count in sorted(event_types.items()):
                            console.print(f"    - {resource}: {count}")
                    else:
                        console.print("  [green]✓ Accessible[/green] (no events yet)")
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    if status == 404:
                        console.print(
                            "  [red]✗ 404 Not Found[/red] - Changelog API may not be enabled"
                        )
                    elif status == 401:
                        console.print("  [red]✗ 401 Unauthorized[/red]")
                    else:
                        console.print(f"  [red]✗ {status} Error[/red]")
                except Exception as e:
                    console.print(f"  [red]✗ Error: {e}[/red]")

        def _find_own_content(conn, target_urn: str) -> str | None:
            """Find content from user's own post/comment matching the target URN.

            Args:
                conn: Database connection
                target_urn: URN of the target post/comment

            Returns:
                Content string if found, None otherwise
            """
            # For activities: look up by post_id in metadata or activity ID
            if target_urn.startswith("urn:li:activity:"):
                activity_id = target_urn.split(":")[-1]
                cursor = conn.execute(
                    """SELECT content FROM items
                       WHERE source_type = 'linkedin'
                       AND metadata LIKE ?""",
                    (f"%{activity_id}%",),
                )
                row = cursor.fetchone()
                if row:
                    return row["content"]

            # For comments: look up by the comment URN
            elif target_urn.startswith("urn:li:comment:"):
                # Try matching on source_id
                cursor = conn.execute(
                    """SELECT content FROM items
                       WHERE source_type = 'linkedin'
                       AND source_id LIKE ?""",
                    (f"%{target_urn}%",),
                )
                row = cursor.fetchone()
                if row:
                    return row["content"]

            return None

        @app.command("enrich")
        def enrich_cmd(
            item_id: Annotated[int, typer.Argument(help="Item ID to enrich")],
            open_url: Annotated[
                bool,
                typer.Option("--open", "-o", help="Open LinkedIn URL in browser"),
            ] = False,
            image: Annotated[
                Path | None,
                typer.Option("--image", "-i", help="Path to screenshot image"),
            ] = None,
        ) -> None:
            """Enrich a reaction or comment with the target post's content.

            Since LinkedIn's API doesn't provide post content for reactions,
            this command lets you manually add context.

            Examples:
                lestash linkedin enrich 1077 --open
                lestash linkedin enrich 1077 --image screenshot.png
                lestash linkedin enrich 1077 --image screenshot.png --open
            """
            from lestash.core.config import Config
            from lestash.core.database import (
                get_cache_dir,
                get_connection,
                get_post_cache,
                upsert_post_cache,
            )
            from lestash.models.item import Item

            config = Config.load()

            with get_connection(config) as conn:
                # Load the item
                cursor = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,))
                row = cursor.fetchone()

                if not row:
                    console.print(f"[red]Item {item_id} not found.[/red]")
                    raise typer.Exit(1)

                item = Item.from_row(row)

                # Check if it's a reaction or comment
                if not item.metadata:
                    console.print("[red]Item has no metadata.[/red]")
                    raise typer.Exit(1)

                target_urn = item.metadata.get("reacted_to") or item.metadata.get("commented_on")
                if not target_urn:
                    console.print(
                        "[red]Item is not a reaction or comment "
                        "(no 'reacted_to' or 'commented_on' in metadata).[/red]"
                    )
                    raise typer.Exit(1)

                # Detect if target is a comment or post
                is_comment = target_urn.startswith("urn:li:comment:")
                target_type = "comment" if is_comment else "post"

                # Show current item info
                console.print(f"[bold]Item {item_id}:[/bold] {item.content}")
                console.print(f"[bold]Target ({target_type}):[/bold] {target_urn}")

                # Generate URL from URN
                target_url = None
                if target_urn.startswith("urn:li:activity:"):
                    target_url = f"https://www.linkedin.com/feed/update/{target_urn}"
                elif is_comment:
                    # Extract parent activity from comment URN
                    # Format: urn:li:comment:(activity:PARENT_ID,COMMENT_ID)
                    match = re.search(r"activity:(\d+)", target_urn)
                    if match:
                        parent_activity = match.group(1)
                        parent_urn = f"urn:li:activity:{parent_activity}"
                        # Include comment URN as query param for potential direct linking
                        from urllib.parse import quote

                        target_url = f"https://www.linkedin.com/feed/update/{parent_urn}?commentUrn={quote(target_urn)}"

                if target_url:
                    console.print(f"[bold]Target URL:[/bold] {target_url}")

                # Check existing cache
                cached = get_post_cache(conn, target_urn)
                if cached:
                    console.print("\n[yellow]Existing cache found:[/yellow]")
                    if cached.get("content_preview"):
                        console.print(f"  Content: {cached['content_preview'][:100]}...")
                    if cached.get("author_name"):
                        console.print(f"  Author: {cached['author_name']}")
                    if cached.get("image_path"):
                        console.print(f"  Image: {cached['image_path']}")

                    if not Confirm.ask("Overwrite existing cache?", default=False):
                        console.print("[dim]Cancelled.[/dim]")
                        return

                # Open URL in browser if requested
                if open_url and target_url:
                    console.print("\n[dim]Opening URL in browser...[/dim]")
                    webbrowser.open(target_url)

                # Handle image if provided
                image_path = None
                if image:
                    if not image.exists():
                        console.print(f"[red]Image file not found: {image}[/red]")
                        raise typer.Exit(1)

                    # Copy image to cache directory
                    cache_dir = get_cache_dir(config)
                    # Create a hash-based filename from the URN
                    urn_hash = hashlib.sha256(target_urn.encode()).hexdigest()[:16]
                    ext = image.suffix or ".png"
                    dest_filename = f"{urn_hash}{ext}"
                    dest_path = cache_dir / dest_filename

                    shutil.copy2(image, dest_path)
                    image_path = str(dest_path)
                    console.print(f"[green]Image saved to: {image_path}[/green]")

                # Detect if this is a reaction from someone else to your content
                raw = item.metadata.get("raw", {})
                owner_urn = raw.get("owner")
                actor_urn = raw.get("actor")
                is_reaction_from_other = owner_urn and actor_urn and owner_urn != actor_urn

                # Prompt for content (use comment-aware language)
                console.print()

                content_preview = None
                author_name = None
                reactor_name = None

                if is_reaction_from_other:
                    # Someone else reacted to YOUR content
                    console.print(
                        f"[cyan]This is a reaction from someone else to your {target_type}.[/cyan]"
                    )

                    # Try to auto-fill from your existing content
                    own_content = _find_own_content(conn, target_urn)
                    if own_content:
                        console.print(f"\n[green]Found your {target_type} content:[/green]")
                        preview = own_content[:200]
                        if len(own_content) > 200:
                            preview += "..."
                        console.print(f"[dim]{preview}[/dim]")
                        content_preview = own_content[:500]
                    else:
                        msg = f"Could not find your {target_type} in database."
                        console.print(f"\n[yellow]{msg}[/yellow]")
                        if image_path:
                            msg = f"Image provided. {target_type.title()} text is optional."
                            console.print(f"[dim]{msg}[/dim]")
                        content_preview = Prompt.ask(
                            f"Paste your {target_type} content (or press Enter to skip)", default=""
                        )

                    # Ask for the reactor's name (the person who reacted)
                    reactor_name = Prompt.ask(
                        "Who reacted? (name, or press Enter to skip)", default=""
                    )
                else:
                    # YOU reacted to someone else's content
                    if image_path:
                        msg = f"Image provided. {target_type.title()} text is optional."
                        console.print(f"[dim]{msg}[/dim]")

                    content_prompt = f"Paste {target_type} content (or press Enter to skip)"
                    content_preview = Prompt.ask(content_prompt, default="")

                    # Prompt for author name
                    author_prompt = f"{target_type.title()} author name (or press Enter to skip)"
                    author_name = Prompt.ask(author_prompt, default="")

                # Validate we have at least one of image or content
                if not content_preview and not image_path:
                    console.print("[red]No content or image provided. Nothing to save.[/red]")
                    raise typer.Exit(1)

                # Determine source type
                source = "manual"
                if image_path and not content_preview:
                    source = "image"
                elif is_reaction_from_other and _find_own_content(conn, target_urn):
                    source = "own_content"

                # Save to cache
                # For reactions from others: author_name is empty (it's your content),
                # reactor_name stores who reacted
                upsert_post_cache(
                    conn,
                    urn=target_urn,
                    content_preview=content_preview if content_preview else None,
                    author_name=author_name if author_name else None,
                    image_path=image_path,
                    url=target_url,
                    source=source,
                    reactor_name=reactor_name if reactor_name else None,
                )

                if is_reaction_from_other and reactor_name:
                    msg = f"{target_type.title()} cache updated with reactor: {reactor_name}"
                    console.print(f"\n[green]{msg}[/green]")
                else:
                    msg = f"{target_type.title()} cache updated for {target_urn}"
                    console.print(f"\n[green]{msg}[/green]")

        @app.command("auto-enrich")
        def auto_enrich_cmd(
            dry_run: Annotated[
                bool,
                typer.Option("--dry-run", "-n", help="Show what would be enriched without saving"),
            ] = False,
        ) -> None:
            """Auto-enrich reactions to your own posts.

            Scans your reactions and checks if any target posts exist in the database
            (i.e., posts you created). If found, automatically caches the post content.

            This is useful after syncing both posts and reactions from LinkedIn.
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection, get_post_cache, upsert_post_cache

            config = Config.load()
            enriched_count = 0
            skipped_count = 0

            with get_connection(config) as conn:
                # Get all reactions and comments from LinkedIn
                cursor = conn.execute(
                    """
                    SELECT id, content, metadata FROM items
                    WHERE source_type = 'linkedin'
                    AND metadata IS NOT NULL
                    """
                )
                items = cursor.fetchall()

                console.print(f"[dim]Scanning {len(items)} LinkedIn items...[/dim]")

                for row in items:
                    item_id = row["id"]
                    metadata_str = row["metadata"]
                    if not metadata_str:
                        continue

                    try:
                        metadata = json.loads(metadata_str)
                    except json.JSONDecodeError:
                        continue

                    # Check if this is a reaction or comment
                    target_urn = metadata.get("reacted_to") or metadata.get("commented_on")
                    if not target_urn:
                        continue

                    # Check if already cached
                    cached = get_post_cache(conn, target_urn)
                    if cached and (cached.get("content_preview") or cached.get("image_path")):
                        skipped_count += 1
                        continue

                    # Try to find the target post in our database
                    # Posts have metadata.post_id or the URN might be in source_id
                    post_cursor = conn.execute(
                        """
                        SELECT content, author, metadata FROM items
                        WHERE source_type = 'linkedin'
                        AND (
                            metadata LIKE ?
                            OR source_id LIKE ?
                        )
                        """,
                        (f'%"post_id": "{target_urn}"%', f"%{target_urn}%"),
                    )
                    post_row = post_cursor.fetchone()

                    # Also try matching on the activity ID part
                    # Extract activity ID from URN like "urn:li:activity:7421941760257556482"
                    if not post_row and target_urn.startswith("urn:li:activity:"):
                        activity_id = target_urn.split(":")[-1]
                        post_cursor = conn.execute(
                            """
                                SELECT content, author, metadata FROM items
                                WHERE source_type = 'linkedin'
                                AND metadata LIKE ?
                                """,
                            (f"%{activity_id}%",),
                        )
                        post_row = post_cursor.fetchone()

                    if post_row:
                        post_content = post_row["content"]
                        post_author = post_row["author"]

                        # Get author name if available
                        author_name = None
                        if post_author:
                            from lestash.core.database import get_person_profile

                            profile = get_person_profile(conn, post_author)
                            if profile:
                                author_name = profile.get("display_name")

                        # Generate preview (first 500 chars)
                        content_preview = post_content[:500] if post_content else None

                        # Generate URL
                        url = None
                        if target_urn.startswith("urn:li:activity:"):
                            url = f"https://www.linkedin.com/feed/update/{target_urn}"

                        if dry_run:
                            console.print(
                                f"[dim]Would enrich item {item_id} with post: "
                                f"{content_preview[:50] if content_preview else 'N/A'}...[/dim]"
                            )
                        else:
                            upsert_post_cache(
                                conn,
                                urn=target_urn,
                                content_preview=content_preview,
                                author_urn=post_author,
                                author_name=author_name,
                                url=url,
                                source="own_post",
                            )
                            console.print(
                                f"[green]Enriched item {item_id}[/green] with own post content"
                            )

                        enriched_count += 1

            if dry_run:
                console.print(f"\n[yellow]Dry run: would enrich {enriched_count} items[/yellow]")
            else:
                console.print(f"\n[green]Auto-enriched {enriched_count} items[/green]")

            if skipped_count > 0:
                console.print(f"[dim]Skipped {skipped_count} already cached items[/dim]")

        @app.command("enrich-all")
        def enrich_all_cmd(
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Maximum items to process"),
            ] = 20,
            skip_comments: Annotated[
                bool,
                typer.Option("--skip-comments", help="Skip comment reactions"),
            ] = False,
        ) -> None:
            """Interactively enrich multiple reactions/comments.

            Goes through un-enriched items one by one, opening URLs and
            prompting for content/author.
            """
            from lestash.core.config import Config
            from lestash.core.database import get_connection, get_post_cache, upsert_post_cache
            from lestash.models.item import Item

            config = Config.load()

            with get_connection(config) as conn:
                # Find reactions/comments without cached content
                # Exclude where owner != actor (already auto-enrichable)
                query = """
                    SELECT * FROM items
                    WHERE source_type = 'linkedin'
                    AND (
                        json_extract(metadata, '$.resource_name') = 'socialActions/likes'
                        OR json_extract(metadata, '$.resource_name') = 'socialActions/comments'
                    )
                    AND json_extract(metadata, '$.raw.owner') =
                        json_extract(metadata, '$.raw.actor')
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                cursor = conn.execute(query, (limit * 3,))  # Over-fetch to filter
                rows = cursor.fetchall()

                # Filter to items without cache
                unenriched = []
                for row in rows:
                    item = Item.from_row(row)
                    if not item.metadata:
                        continue
                    target_urn = item.metadata.get("reacted_to") or item.metadata.get(
                        "commented_on"
                    )
                    if target_urn and isinstance(target_urn, str):
                        cached = get_post_cache(conn, target_urn)
                        if not cached or not cached.get("content_preview"):
                            if skip_comments and target_urn.startswith("urn:li:comment:"):
                                continue
                            unenriched.append(item)
                    if len(unenriched) >= limit:
                        break

                if not unenriched:
                    console.print("[green]No un-enriched items found![/green]")
                    return

                console.print(f"[bold]Found {len(unenriched)} items to enrich[/bold]\n")

                enriched_count = 0
                for i, item in enumerate(unenriched, 1):
                    # Already filtered to items with metadata and target_urn
                    assert item.metadata is not None
                    _urn = item.metadata.get("reacted_to") or item.metadata.get("commented_on")
                    assert isinstance(_urn, str)
                    target_urn = _urn
                    is_comment = target_urn.startswith("urn:li:comment:")
                    target_type = "comment" if is_comment else "post"

                    # Build URL
                    if target_urn.startswith("urn:li:activity:"):
                        target_url = f"https://www.linkedin.com/feed/update/{target_urn}"
                    elif is_comment:
                        match = re.search(r"activity:(\d+)", target_urn)
                        if match:
                            parent_urn = f"urn:li:activity:{match.group(1)}"
                            target_url = f"https://www.linkedin.com/feed/update/{parent_urn}"
                        else:
                            target_url = None
                    else:
                        target_url = None

                    # Show item info
                    console.print(f"\n[bold cyan]Item {i}/{len(unenriched)}[/bold cyan]")
                    console.print(f"[bold]ID:[/bold] {item.id}")
                    console.print(f"[bold]Content:[/bold] {item.content[:80]}...")
                    console.print(f"[bold]Type:[/bold] {target_type}")
                    if target_url:
                        console.print(f"[bold]URL:[/bold] {target_url}")

                    # Ask what to do
                    action = Prompt.ask(
                        "\n[bold]Action[/bold]",
                        choices=["open", "skip", "enrich", "quit"],
                        default="open",
                    )

                    if action == "quit":
                        break
                    elif action == "skip":
                        continue
                    elif action == "open":  # noqa: SIM102
                        if target_url:
                            webbrowser.open(target_url)
                        # Fall through to enrich

                    # For your reactions: just ask for author name
                    author = Prompt.ask(
                        f"{target_type.title()} author name (or Enter to skip)", default=""
                    )

                    if author:
                        upsert_post_cache(
                            conn,
                            urn=target_urn,
                            author_name=author,
                            url=target_url,
                            source="manual",
                        )
                        enriched_count += 1
                        console.print("[green]Saved![/green]")
                    else:
                        console.print("[yellow]Skipped (no author provided)[/yellow]")

                console.print(f"\n[bold green]Enriched {enriched_count} items[/bold green]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync LinkedIn content.

        Uses the DMA Portability API Changelog endpoint to fetch recent activity
        (posts, comments, reactions from the past 28 days).
        Requires prior authentication with 'lestash linkedin auth'.
        """
        token = load_token()
        if not token:
            logger.warning("No token found for LinkedIn sync")
            return

        access_token = token.get("access_token")
        if not access_token:
            logger.warning("Invalid token for LinkedIn sync")
            return

        with LinkedInAPI(access_token) as api:
            events = api.get_all_changelog()
            yield from changelog_to_items(events)

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "mode": "self-serve",  # or "3rd-party"
        }

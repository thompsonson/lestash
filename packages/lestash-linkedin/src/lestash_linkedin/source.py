"""LinkedIn source plugin implementation."""

import json
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

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync LinkedIn content.

        Uses the DMA Portability API (self-serve or 3rd-party mode).
        Requires prior authentication with 'lestash linkedin auth'.
        """
        return iter([])

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "mode": "self-serve",  # or "3rd-party"
        }

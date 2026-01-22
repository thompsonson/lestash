"""CLI commands for managing person profiles."""

from typing import Annotated

import typer
from rich.table import Table

from lestash.core.database import (
    delete_person_profile,
    get_connection,
    list_person_profiles,
    upsert_person_profile,
)
from lestash.core.logging import get_console

app = typer.Typer(help="Manage person profile mappings (URN to profile URL).")
console = get_console()


@app.command("list")
def list_profiles() -> None:
    """List all person profile mappings."""
    with get_connection() as conn:
        profiles = list_person_profiles(conn)

    if not profiles:
        console.print("[dim]No profiles configured.[/dim]")
        console.print("\nAdd a profile with: lestash profiles add <urn> --url <profile-url>")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("URN", style="dim")
    table.add_column("Profile URL")
    table.add_column("Name")
    table.add_column("Source", style="dim")

    for profile in profiles:
        table.add_row(
            profile["urn"],
            profile["profile_url"] or "-",
            profile["display_name"] or "-",
            profile["source"],
        )

    console.print(table)


@app.command("add")
def add_profile(
    urn: Annotated[str, typer.Argument(help="Person URN (e.g., urn:li:person:xu59iSkkD6)")],
    url: Annotated[str | None, typer.Option("--url", "-u", help="LinkedIn profile URL")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Display name")] = None,
) -> None:
    """Add or update a person profile mapping."""
    if not url and not name:
        console.print("[red]Error: Must provide at least --url or --name[/red]")
        raise typer.Exit(1)

    with get_connection() as conn:
        upsert_person_profile(conn, urn, profile_url=url, display_name=name)

    console.print(f"[green]Profile added/updated for {urn}[/green]")


@app.command("remove")
def remove_profile(
    urn: Annotated[str, typer.Argument(help="Person URN to remove")],
) -> None:
    """Remove a person profile mapping."""
    with get_connection() as conn:
        deleted = delete_person_profile(conn, urn)

    if deleted:
        console.print(f"[green]Profile removed for {urn}[/green]")
    else:
        console.print(f"[yellow]No profile found for {urn}[/yellow]")


@app.command("show")
def show_profile(
    urn: Annotated[str, typer.Argument(help="Person URN to look up")],
) -> None:
    """Show details for a specific profile."""
    from lestash.core.database import get_person_profile

    with get_connection() as conn:
        profile = get_person_profile(conn, urn)

    if not profile:
        console.print(f"[yellow]No profile found for {urn}[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold]URN:[/bold] {profile['urn']}")
    console.print(f"[bold]Profile URL:[/bold] {profile['profile_url'] or '-'}")
    console.print(f"[bold]Display Name:[/bold] {profile['display_name'] or '-'}")
    console.print(f"[bold]Source:[/bold] {profile['source']}")

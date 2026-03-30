"""Google integration CLI commands — auth, doctor, and Drive download."""

import typer
from rich.console import Console

app = typer.Typer(help="Google account integration (Drive, Docs).", no_args_is_help=True)
console = Console()


@app.command()
def auth(
    headless: bool = typer.Option(False, "--headless", help="Force console-based auth (for SSH)"),
) -> None:
    """Authenticate with Google (opens browser or prints URL for SSH)."""
    from lestash.core.google_auth import is_headless, run_auth_flow

    use_headless = headless or is_headless()
    if use_headless:
        console.print("[dim]SSH detected — using console auth flow.[/dim]")
        console.print("[dim]A URL will be printed. Open it in your local browser,[/dim]")
        console.print("[dim]authorize, then paste the code back here.[/dim]\n")

    try:
        credentials = run_auth_flow(headless=use_headless)
        console.print("\n[green]✓[/green] Authenticated successfully.")
        if credentials.scopes:
            console.print(f"  Scopes: {', '.join(credentials.scopes)}")
    except FileNotFoundError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]✗[/red] Auth failed: {e}")
        raise typer.Exit(1) from None


@app.command()
def doctor() -> None:
    """Check Google auth status."""
    from lestash.core.google_auth import (
        check_auth_status,
        get_client_secrets_path,
        get_credentials_path,
    )

    status = check_auth_status()

    console.print("[bold]Google Auth Status[/bold]\n")

    if status["client_secrets_exists"]:
        console.print(f"  [green]✓[/green] Client secrets: {get_client_secrets_path()}")
    else:
        console.print(f"  [red]✗[/red] Client secrets missing: {get_client_secrets_path()}")
        console.print("    Download from https://console.cloud.google.com/apis/credentials")
        return

    if status["credentials_exists"]:
        console.print(f"  [green]✓[/green] Credentials: {get_credentials_path()}")
    else:
        console.print("  [yellow]○[/yellow] No credentials — run 'lestash google auth'")
        return

    if status["authenticated"]:
        console.print("  [green]✓[/green] Authenticated")
        scopes = status.get("scopes", [])
        if scopes:
            for scope in scopes:
                console.print(f"    • {scope}")
    else:
        console.print("  [red]✗[/red] Not authenticated — run 'lestash google auth'")
        if status.get("refresh_error"):
            console.print(f"    Refresh error: {status['refresh_error']}")


@app.command()
def download(
    url: str = typer.Argument(help="Google Drive URL or file ID"),
    output: str = typer.Option(None, "--output", "-o", help="Output directory"),
) -> None:
    """Download a file from Google Drive."""
    from pathlib import Path

    from lestash.core.google_auth import download_drive_file, extract_drive_file_id

    file_id = extract_drive_file_id(url)
    console.print(f"Downloading file [dim]{file_id}[/dim]...")

    try:
        output_dir = Path(output) if output else None
        path = download_drive_file(file_id, output_dir)
        console.print(f"[green]✓[/green] Saved to {path}")
        console.print(f"  Size: {path.stat().st_size / 1024 / 1024:.1f} MB")
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]✗[/red] Download failed: {e}")
        raise typer.Exit(1) from None

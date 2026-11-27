"""Google integration CLI commands — auth, doctor, Drive download, and folder sync."""

import typer
from rich.console import Console
from rich.table import Table

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


@app.command("ls")
def list_folder(
    folder: str = typer.Argument(help="Google Drive folder URL or ID"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="List subfolders recursively"),
) -> None:
    """List files in a Google Drive folder."""
    from lestash.core.google_auth import get_drive_service
    from lestash.core.google_drive import extract_folder_id, list_drive_folder

    folder_id = extract_folder_id(folder)
    service = get_drive_service()
    files = list_drive_folder(service, folder_id, recursive=recursive)

    table = Table(title=f"Drive folder ({len(files)} items)")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="dim")
    table.add_column("Size", justify="right")
    table.add_column("Modified", style="dim")

    for f in files:
        size = int(f.get("size", 0))
        size_str = f"{size / 1024:.0f} KB" if size > 0 else "—"
        mime = f.get("mimeType", "")
        short_type = mime.split("/")[-1].split(".")[-1][:10]
        modified = (f.get("modifiedTime") or "")[:10]
        path = f.get("folder_path", "")
        name = f"{path}/{f['name']}" if path else f["name"]
        table.add_row(name, short_type, size_str, modified)

    console.print(table)


@app.command()
def sync(
    folder: str = typer.Argument(help="Google Drive folder URL or ID"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List files without importing"),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Recurse into subfolders"),
    since: str | None = typer.Option(
        None, "--since", help="Only sync files modified after this ISO date"
    ),
) -> None:
    """Import files from a Google Drive folder into LeStash.

    Downloads each file, converts to markdown via Docling, and stores
    as a searchable item with source_type 'google-drive'.
    """
    from lestash.core.config import Config
    from lestash.core.database import get_connection
    from lestash.core.google_auth import get_drive_service
    from lestash.core.google_drive import (
        FOLDER_MIME,
        SKIP_MIME_PREFIXES,
        extract_folder_id,
        list_drive_folder,
        store_folder_file,
        sync_drive_folder,
    )

    folder_id = extract_folder_id(folder)

    if dry_run:
        service = get_drive_service()
        files = list_drive_folder(service, folder_id, since=since, recursive=recursive)
        files = [
            f
            for f in files
            if f.get("mimeType") != FOLDER_MIME
            and not any(f.get("mimeType", "").startswith(p) for p in SKIP_MIME_PREFIXES)
        ]
        console.print(f"\n[bold]Dry run:[/bold] {len(files)} files would be imported\n")
        for f in files:
            size = int(f.get("size", 0))
            size_str = f"{size / 1024:.0f} KB" if size > 0 else "—"
            path = f.get("folder_path", "")
            name = f"{path}/{f['name']}" if path else f["name"]
            console.print(f"  {name}  [dim]({size_str})[/dim]")
        return

    from lestash.core.doc_pictures import attach_pictures
    from lestash.core.pdf_enrich import attach_source_pdf_and_enrich
    from lestash.core.text_extract import DocPicture

    config = Config.load()
    added = 0
    enriched = 0
    pictured = 0
    errors: list[str] = []
    pdf_followups: list[tuple[int, bytes, str, str | None]] = []
    picture_followups: list[tuple[int, list[DocPicture | None]]] = []

    with (
        console.status("[bold]Syncing Google Drive folder...[/bold]") as status,
        get_connection(config) as conn,
    ):
        for drive_file in sync_drive_folder(folder_id, since=since, recursive=recursive):
            item = drive_file.item
            status.update(f"Processing: {item.title or 'unknown'}")
            try:
                item_id = store_folder_file(conn, item)
                added += 1
                if drive_file.pdf_bytes:
                    drive_url = (item.metadata or {}).get("drive_web_link")
                    pdf_followups.append(
                        (item_id, drive_file.pdf_bytes, drive_file.filename, drive_url)
                    )
                if drive_file.pictures:
                    picture_followups.append((item_id, drive_file.pictures))
            except Exception as e:
                errors.append(f"{item.title}: {e}")
        conn.commit()

    for item_id, pictures in picture_followups:
        try:
            pictured += attach_pictures(item_id, pictures, config=config)
        except Exception as e:
            errors.append(f"pictures for item {item_id}: {e}")

    for item_id, pdf_bytes, filename, drive_url in pdf_followups:
        try:
            result = attach_source_pdf_and_enrich(
                item_id, pdf_bytes, filename, drive_url=drive_url, config=config
            )
            if result.status == "enriched":
                enriched += 1
        except Exception as e:
            errors.append(f"enrich item {item_id}: {e}")

    console.print(f"\n[green]✓[/green] Imported {added} items from Google Drive")
    if enriched:
        console.print(f"[green]✓[/green] Enriched {enriched} PDFs")
    if pictured:
        console.print(f"[green]✓[/green] Saved {pictured} pictures from documents")
    if errors:
        console.print(f"[yellow]  {len(errors)} errors:[/yellow]")
        for err in errors:
            console.print(f"    • {err}")


@app.command()
def pictures(
    dry_run: bool = typer.Option(False, "--dry-run", help="List the items without fetching them"),
) -> None:
    """Fetch Drive Word files and Google Docs again to fill their pictures (#394).

    Finds Drive items whose content still has empty `<!-- image -->` markers,
    imports each one again from Drive, and saves its pictures in their places.
    """
    from lestash.core.config import Config
    from lestash.core.database import get_connection, upsert_item, write_transaction
    from lestash.core.doc_pictures import attach_pictures
    from lestash.core.google_drive import GOOGLE_DOC_MIME, sync_single_file
    from lestash.core.pdf_enrich.images import count_placeholders
    from lestash.core.text_extract import DOCX_MIME_TYPES

    config = Config.load()
    word_types = sorted({GOOGLE_DOC_MIME, *DOCX_MIME_TYPES})
    with get_connection(config) as conn:
        rows = conn.execute(
            f"""
            SELECT id, source_id, title FROM items
            WHERE source_type = 'google-drive' AND content LIKE '%<!-- image -->%'
              AND json_extract(metadata, '$.mime_type') IN ({",".join("?" * len(word_types))})
            ORDER BY id
            """,
            word_types,
        ).fetchall()

    console.print(f"[bold]{len(rows)}[/bold] Drive documents have empty picture markers")
    if dry_run:
        for row in rows:
            console.print(f"  {row['id']}  {row['title']}")
        return

    filled = 0
    saved = 0
    errors: list[str] = []
    for row in rows:
        try:
            drive_file = sync_single_file(row["source_id"])
            if not drive_file.pictures or not count_placeholders(drive_file.item.content):
                # Unreadable now, or its pictures are gone: leave the item as it is
                errors.append(f"item {row['id']}: no pictures found when fetched again")
                continue
            with get_connection(config) as conn, write_transaction(conn):
                upsert_item(conn, drive_file.item)
            count = attach_pictures(row["id"], drive_file.pictures, config=config)
            if count:
                filled += 1
                saved += count
        except Exception as e:
            errors.append(f"item {row['id']}: {e}")

    console.print(f"[green]✓[/green] Saved {saved} pictures in {filled} documents")
    if errors:
        console.print(f"[yellow]  {len(errors)} errors:[/yellow]")
        for err in errors:
            console.print(f"    • {err}")

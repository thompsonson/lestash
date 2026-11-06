"""Google Takeout source plugin.

Set Takeout's delivery method to "Add to Drive" (optionally "export every 2 months"). Each sync
lists Takeout zips in Drive, imports the ones not seen before with LeStash's zip importer
(Keep, Gemini, NotebookLM, ...), and deletes the downloaded copy. Uses LeStash's Google sign-in
with read-only Drive access; nothing is changed in Drive.
"""

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_takeout.drive import download, forget, list_takeout_zips, load_state, mark_imported

console = Console()
logger = get_plugin_logger("takeout")


def _drive_service() -> Any:
    from lestash.core.google_auth import get_drive_service

    return get_drive_service()


def import_zip(
    service: Any, file: dict[str, Any], workdir: Path | None = None
) -> Iterator[ItemCreate]:
    """Download one Takeout zip, yield its items, then record it as imported.

    The zip is deleted afterwards. It is only marked imported once every item has been
    consumed, so an interrupted sync retries it next time.
    """
    from lestash_server.routes.imports import parse_zip

    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        path = download(service, file["id"], Path(tmp) / file["name"])
        try:
            items, source_type = parse_zip(path)
        except ValueError as e:
            # Nothing LeStash understands (e.g. a part with only photos): don't retry it.
            logger.info(f"{file['name']}: {e}")
            mark_imported(file, 0, "unrecognized")
            return
    yield from items
    mark_imported(file, len(items), source_type)
    logger.info(f"{file['name']}: {len(items)} items ({source_type})")


def pending_zips(service: Any) -> list[dict[str, Any]]:
    imported = load_state()["imported"]
    return [f for f in list_takeout_zips(service) if f["id"] not in imported]


class TakeoutSource(SourcePlugin):
    """Google Takeout (delivered to Drive) source plugin."""

    name = "takeout"
    description = "Google Takeout zips delivered to Drive"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Google Takeout (delivered to Drive) commands.")

        @app.command("list")
        def list_cmd() -> None:
            """Show Takeout zips in Drive and whether they've been imported."""
            imported = load_state()["imported"]
            table = Table(title="Takeout zips in Drive")
            for col in ("Name", "Size", "Created", "Imported", "ID"):
                table.add_column(col)
            for f in list_takeout_zips(_drive_service()):
                seen = imported.get(f["id"])
                status = f"{seen['items']} items, {seen['imported_at'][:10]}" if seen else "pending"
                table.add_row(
                    f["name"],
                    f"{int(f.get('size', 0)) / 1e6:,.0f} MB",
                    f.get("createdTime", "")[:10],
                    status,
                    f["id"],
                )
            console.print(table)

        @app.command("forget")
        def forget_cmd(
            file_id: Annotated[str, typer.Argument(help="Drive file id (see `list`)")],
        ) -> None:
            """Mark a zip as not imported, so the next sync imports it again."""
            console.print("Forgotten." if forget(file_id) else "That id wasn't marked imported.")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        try:
            service = _drive_service()
        except ValueError as e:
            logger.warning(f"Google Drive not available: {e}")
            return
        for file in pending_zips(service):
            yield from import_zip(service, file)

    def configure(self) -> dict:
        return {}

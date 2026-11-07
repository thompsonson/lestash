"""Kobo eReader source plugin.

Two steps, kept apart so the device is unplugged as soon as possible:

1. ``lestash kobo snapshot`` copies the mounted Kobo into a versioned store.
2. ``lestash sources sync kobo`` reads the newest snapshot into LeStash.

Nothing ever writes to the Kobo: it is mounted read-only (see deploy/kobo/).
"""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from lestash.core.config import Config
from lestash.core.database import get_media_dir
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_kobo.reader import KoboReader
from lestash_kobo.store import KoboStore, SnapshotResult

console = Console()
logger = get_plugin_logger("kobo")

DEFAULT_MOUNT = Path("/media/kobo")


def _store(config: dict) -> KoboStore:
    return KoboStore(config.get("store_dir"))


def _report(result: SnapshotResult) -> None:
    snap = result.snapshot
    console.print(
        f"Snapshot {snap.name}: {result.files} files, "
        f"{result.new_blobs} new ({result.new_bytes / 1e6:.1f} MB)"
    )
    for label, paths in (
        ("added", result.added),
        ("changed", result.changed),
        ("removed", result.removed),
    ):
        if paths:
            console.print(f"  {len(paths)} {label}")
            for p in paths[:20]:
                console.print(f"    {p}")
    for p in result.reverted:
        console.print(f"[red]  {p} went back to its first version: annotations wiped?[/red]")


class KoboSource(SourcePlugin):
    """Kobo eReader: books, highlights, notes and pen markups."""

    name = "kobo"
    description = "Kobo eReader books, highlights, notes and markups"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Kobo eReader commands.")

        @app.command("snapshot")
        def snapshot_cmd(
            source: Annotated[
                Path,
                typer.Option("--from", help="The Kobo's root folder (a mount or a copy)"),
            ] = DEFAULT_MOUNT,
            taken_at: Annotated[
                str | None,
                typer.Option(
                    "--taken-at", help="When the copy was made (ISO time), for old copies"
                ),
            ] = None,
        ) -> None:
            """Copy the Kobo into the snapshot store. Unchanged files cost nothing."""
            store = _store(Config.load().get_plugin_config(self.name))
            when = datetime.fromisoformat(taken_at) if taken_at else None
            try:
                result = store.take(source, when)
            except (FileNotFoundError, FileExistsError) as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(1) from e
            _report(result)

        @app.command("snapshots")
        def snapshots_cmd() -> None:
            """List snapshots, oldest first."""
            store = _store(Config.load().get_plugin_config(self.name))
            snaps = store.snapshots()
            if not snaps:
                console.print(f"No snapshots in {store.root}")
                return
            for s in snaps:
                size = sum(f.size for f in s.files.values())
                console.print(f"{s.name}  {len(s.files):5d} files  {size / 1e6:8.1f} MB")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        store = _store(config)
        snapshot = store.latest()
        if snapshot is None:
            logger.warning(
                "No Kobo snapshot in %s, so nothing was synced. Plug in the Kobo, or run "
                "`lestash kobo snapshot --from <folder>`.",
                store.root,
            )
            return
        logger.info("Reading Kobo snapshot %s", snapshot.name)
        reader = KoboReader(
            store,
            snapshot,
            media_dir=get_media_dir(),
            include_store_titles=bool(config.get("include_store_titles", False)),
        )
        yield from reader.items()

"""Kobo eReader source plugin.

Two steps, kept apart so the device is unplugged as soon as possible:

1. ``lestash kobo snapshot`` copies the mounted Kobo into a versioned store.
2. ``lestash sources sync kobo`` reads the newest snapshot into LeStash.

Nothing ever writes to the Kobo: it is mounted read-only (see deploy/99-lestash-kobo.rules).
"""

import fcntl
import signal
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from lestash.core.config import Config
from lestash.core.database import get_checkpoints, get_connection, get_media_dir
from lestash.core.logging import get_plugin_logger
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint
from lestash.plugins.base import SourcePlugin
from rich.console import Console

from lestash_kobo.reader import READER_VERSION, KoboReader
from lestash_kobo.store import KoboStore, SnapshotResult

console = Console()
logger = get_plugin_logger("kobo")

DEFAULT_MOUNT = Path("/media/kobo")
STATE_KEY = "imported"


def flag(value: object) -> bool:
    """A config switch. ``lestash config set`` stores strings, so "false" must be False."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def parse_taken_at(value: str) -> datetime:
    """An ISO time; without an offset it is read as UTC, not local time."""
    try:
        when = datetime.fromisoformat(value)
    except ValueError as e:
        raise typer.BadParameter(f"not an ISO time: {value!r}") from e
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _raise_on_sigterm(signum: int, frame: object) -> None:
    # systemd stops the plug-in unit with SIGTERM when the Kobo is unplugged. Turning it
    # into SystemExit lets the snapshot remove its temp files on the way out.
    raise SystemExit(128 + signum)


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
            verify: Annotated[
                bool, typer.Option("--verify", help="Re-read every file, not just changed ones")
            ] = False,
        ) -> None:
            """Copy the Kobo into the snapshot store. Unchanged files cost nothing."""
            store = _store(Config.load().get_plugin_config(self.name))
            when = parse_taken_at(taken_at) if taken_at else None
            signal.signal(signal.SIGTERM, _raise_on_sigterm)
            try:
                result = store.take(source, when, verify=verify)
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

    def sync(self, config: dict) -> Iterator[ItemCreate | SyncCheckpoint]:
        store = _store(config)
        include_store = flag(config.get("include_store_titles"))
        if not store.root.is_dir():
            logger.info("No Kobo snapshots yet in %s; nothing to import", store.root)
            return
        # The plug-in import and the 6-hourly timer (which syncs every source) can start
        # together. The second waits, then finds the newest snapshot itself: if the first
        # already imported it, the checkpoint makes the second a no-op.
        with open(store.root / ".sync.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            imported: str | None = None
            # A plug-in during this import adds a snapshot; import again until the
            # newest one is in.
            while True:
                snapshot, newest_name = store.newest()
                if newest_name is None:
                    logger.warning(
                        "No Kobo snapshot in %s, so nothing was synced. Plug in the Kobo, "
                        "or run `lestash kobo snapshot --from <folder>`.",
                        store.root,
                    )
                    return
                if snapshot is None:
                    # Importing an older snapshot instead would overwrite newer data
                    raise RuntimeError(
                        f"The newest Kobo snapshot ({newest_name}) is unreadable, so "
                        "nothing was imported. Remove or repair it."
                    )
                digest = snapshot.digest()
                if digest == imported:
                    return
                # Keyed on the contents, not the name: plugging in twice without reading
                # makes a new snapshot with the same files, which needs no re-import
                state = {
                    "snapshot_digest": digest,
                    "reader_version": READER_VERSION,
                    "include_store_titles": include_store,
                }
                with get_connection(Config.load()) as conn:
                    if get_checkpoints(conn, self.name).get(STATE_KEY) == state:
                        logger.info("Kobo snapshot %s is already imported", snapshot.name)
                        return
                logger.info("Reading Kobo snapshot %s", snapshot.name)
                reader = KoboReader(
                    store, snapshot, media_dir=get_media_dir(), include_store_titles=include_store
                )
                yield from reader.items()
                yield SyncCheckpoint(key=STATE_KEY, value=state)
                imported = digest

"""``lestash kobo collections``, ``show``, ``hide`` and ``write-collections`` (K8, #315)."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from lestash.core.collections import (
    KOBO,
    Shelf,
    kobo_shelves,
    kobo_status,
    record_error,
    record_written,
    set_shown,
)
from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.core.sync import SyncAlreadyRunning, SyncRun
from rich.console import Console

from lestash_kobo import shelves as kobo_shelf
from lestash_kobo.store import KoboStore

console = Console()

LOG_SOURCE = "kobo-collections"
RW_MOUNT = Path("/media/kobo-rw")
RO_MOUNT = Path("/media/kobo")
MOUNT, UMOUNT, MOUNTPOINT = "/usr/bin/mount", "/usr/bin/umount", "/usr/bin/mountpoint"


def _mounted(path: Path) -> bool:
    return subprocess.run([MOUNTPOINT, "-q", str(path)], check=False).returncode == 0


def _collection_id(conn: sqlite3.Connection, name: str) -> int:
    rows = conn.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchall()
    if len(rows) != 1:
        found = "no" if not rows else f"{len(rows)}"
        console.print(f"[red]{found} collections are called {name!r}.[/red]")
        raise typer.Exit(1)
    return rows[0][0]


def add_collection_commands(app: typer.Typer, store_for: Callable[[Config], KoboStore]) -> None:
    """``store_for(config)``: the plugin's snapshot store for the loaded config."""

    @app.command("collections")
    def collections_cmd() -> None:
        """Each collection shown on the Kobo, and how far the Kobo is from it."""
        with get_connection(Config.load()) as conn:
            rows = conn.execute(
                "SELECT c.id, c.name FROM collection_places p JOIN collections c"
                " ON c.id = p.collection_id WHERE p.place = ? ORDER BY c.name",
                (KOBO,),
            ).fetchall()
            if not rows:
                console.print("No collection is shown on the Kobo. Use `lestash kobo show NAME`.")
                return
            for cid, name in rows:
                st = kobo_status(conn, cid)
                assert st is not None
                state = "on" if st.shown else "off (removed at the next plug-in)"
                when = f", last written {st.last_written}" if st.last_written else ""
                console.print(f"{name}: {state}; {st.text()}{when}")
                if st.error:
                    console.print(f"  [red]{st.error}[/red]")

    def _switch(name: str, shown: bool) -> None:
        with get_connection(Config.load()) as conn:
            set_shown(conn, _collection_id(conn, name), KOBO, shown)
            conn.commit()
        console.print(
            f"{name!r} will be {'on' if shown else 'taken off'} the Kobo at the next plug-in."
        )

    @app.command("show")
    def show_cmd(name: Annotated[str, typer.Argument(help="The collection's name")]) -> None:
        """Show a collection on the Kobo, from the next plug-in."""
        _switch(name, True)

    @app.command("hide")
    def hide_cmd(name: Annotated[str, typer.Argument(help="The collection's name")]) -> None:
        """Take a collection off the Kobo at the next plug-in."""
        _switch(name, False)

    @app.command("write-collections")
    def write_cmd(
        root: Annotated[
            Path, typer.Option("--root", help="The Kobo mounted read-write")
        ] = RW_MOUNT,
        mount: Annotated[
            bool, typer.Option("--mount", help="Mount --root first, and unmount after")
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Say what would change; touch nothing")
        ] = False,
    ) -> None:
        """Write the collections shown on the Kobo. Run by the plug-in unit after the snapshot."""
        # systemd stops the unit with SIGTERM when the Kobo is unplugged: unmount on the way out
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
        config = Config.load()
        store: KoboStore = store_for(config)
        with get_connection(config) as conn:
            shelves = kobo_shelves(conn)
            if not shelves:
                console.print("No collection is shown on the Kobo; nothing to write.")
                return
            snapshot, newest = store.newest()
            if snapshot is None:
                console.print(f"[red]No readable Kobo snapshot ({newest}); nothing written.[/red]")
                raise typer.Exit(1)
            the_plan = kobo_shelf.plan(store, snapshot, shelves)
            _report(the_plan.results, dry_run or the_plan.changes == 0)
            if dry_run:
                return
            try:
                run = SyncRun.start(conn, LOG_SOURCE)
            except SyncAlreadyRunning:
                console.print("[red]Another collections write is running.[/red]")
                raise typer.Exit(1) from None
            with run:
                results = the_plan.results
                if the_plan.changes:
                    try:
                        results = _write(store, the_plan, shelves, root, mount)
                    except (kobo_shelf.KoboChanged, kobo_shelf.KoboDamaged, OSError) as e:
                        for shelf in shelves:
                            record_error(conn, shelf, str(e))
                        conn.commit()
                        run.finish("failed", items_added=0, items_updated=0, error=str(e))
                        console.print(f"[red]Nothing written: {e}[/red]")
                        raise typer.Exit(1) from None
                when = the_plan.when if the_plan.changes else None
                errors = []
                for res in results:
                    if res.error:
                        record_error(conn, res.shelf, res.error)
                        errors.append(res.error)
                    else:
                        record_written(
                            conn,
                            res.shelf,
                            external_id=res.external_id,
                            external_name=res.external_name,
                            written=res.written,
                            when=when,
                        )
                conn.commit()
                run.finish(
                    "completed",
                    items_added=sum(r.added for r in results),
                    items_updated=sum(r.removed for r in results),
                    error="; ".join(errors) or None,
                )


def _write(
    store: KoboStore,
    the_plan: kobo_shelf.Plan,
    shelves: list[Shelf],
    root: Path,
    mount: bool,
) -> list[kobo_shelf.ShelfResult]:
    if not mount:
        return kobo_shelf.write(store, the_plan, shelves, root)
    if _mounted(RO_MOUNT) or _mounted(root):
        raise OSError(f"the Kobo is already mounted ({RO_MOUNT} or {root}); unmount it first")
    subprocess.run([MOUNT, str(root)], check=True, capture_output=True)
    try:
        return kobo_shelf.write(store, the_plan, shelves, root)
    finally:
        subprocess.run(["sync"], check=False)
        subprocess.run([UMOUNT, str(root)], check=False)


def _report(results: list[kobo_shelf.ShelfResult], preview: bool) -> None:
    verb = "would" if preview else "will"
    for r in results:
        name = r.shelf.name or r.shelf.external_name or "(deleted collection)"
        if r.error:
            console.print(f"[red]{name}: not written: {r.error}[/red]")
            continue
        if not r.shelf.shown:
            console.print(f"{name}: {verb} be taken off the Kobo ({r.removed} rows)")
            continue
        parts = [f"{len(r.written)} books"]
        if r.added:
            parts.append(f"{r.added} to add")
        if r.removed:
            parts.append(f"{r.removed} to remove")
        if r.not_on_kobo:
            parts.append(f"{r.not_on_kobo} not on the Kobo yet")
        console.print(f"{name}: {', '.join(parts)}")
    if preview and not any(r.added or r.removed for r in results):
        console.print("The Kobo already shows these; nothing to write.")

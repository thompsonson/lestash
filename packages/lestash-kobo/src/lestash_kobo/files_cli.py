"""``lestash kobo send``, ``unsend``, ``files``, ``copy-files`` and ``extract-notes`` (#338).

At each plug-in, after the collections are written, ``copy-files --mount`` puts the
newest version of each item asked for into ``LeStash/`` on the Kobo. The Kobo imports
them when it is ejected; the next plug-in's import links each book to its item by its
bytes (``alternate_of``), and collections show it from then on.

A file on the Kobo is never overwritten or deleted. A copy counts only once its
SHA-256 is read back from the Kobo and matches.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from lestash.core.collections import KOBO
from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.core.item_places import (
    Wanted,
    asked_for,
    item_place,
    record_copy,
    record_copy_error,
    set_wanted,
    to_copy,
)
from lestash.core.sync import SyncAlreadyRunning, SyncRun
from rich.console import Console

from lestash_kobo.collections_cli import RW_MOUNT, rw_mounted
from lestash_kobo.reader import BOOK_FILES
from lestash_kobo.shelves import now_text
from lestash_kobo.store import KoboStore

console = Console()

LOG_SOURCE = "kobo-files"
NOTES_SOURCE = "kobo-notes"
FOLDER = "LeStash"  # where copies go on the Kobo
PART = ".lestash-part"  # the end of an unfinished copy's hidden name
NAME_CHARS = 120  # the longest file name made, before ".pdf"
SPARE = 50 * 1024 * 1024  # space left free on the Kobo
UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')  # not allowed in a FAT file name


@dataclass
class Copied:
    wanted: Wanted
    path: str | None = None  # relative to the Kobo's root
    made: bool = False  # False: the same bytes were there already
    error: str | None = None


def file_name(title: str) -> str:
    """A FAT-safe file name for a title."""
    name = " ".join(UNSAFE.sub(" ", title).split()).strip(". ")
    return (name[:NAME_CHARS].rstrip(". ") or "Untitled") + ".pdf"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def held_on_kobo(store: KoboStore) -> dict[str, str]:
    """Book files in the newest snapshot, as SHA-256 → path; Drive's copies left out."""
    snapshot = store.latest()
    if snapshot is None:
        return {}
    return {
        entry.sha256: rel
        for rel, entry in sorted(snapshot.files.items())
        if rel.lower().endswith(BOOK_FILES) and not rel.startswith(".")
    }


def _place(root: Path, wanted: Wanted) -> tuple[str, bool]:
    """Where the copy goes, and whether those bytes are there already."""
    name = file_name(wanted.title)
    stem = name[: -len(".pdf")]
    for candidate in (name, f"{stem} ({wanted.sha256[:8]}).pdf"):
        rel = f"{FOLDER}/{candidate}"
        there = root / rel
        if not there.exists():
            return rel, False
        if there.is_file() and _sha(there) == wanted.sha256:
            return rel, True
    raise OSError(f"{FOLDER}/{name} and its other name hold other files; nothing overwritten")


def copy_files(root: Path, wanted: list[Wanted], held: dict[str, str]) -> list[Copied]:
    """Copy each file to the Kobo mounted read-write at ``root``, checking each copy."""
    results = [Copied(w) for w in wanted]
    todo: list[Copied] = []
    for result in results:
        rel = held.get(result.wanted.sha256)
        if rel and (root / rel).is_file():
            result.path = rel  # already there, put there some other way
        else:
            todo.append(result)
    need = sum(r.wanted.file.stat().st_size for r in todo)
    free = shutil.disk_usage(root).free
    if need and need + SPARE > free:
        for result in todo:
            result.error = f"not enough space on the Kobo: {need} bytes wanted, {free} free"
        return results
    for result in todo:
        try:
            rel, there = _place(root, result.wanted)
            result.path = rel
            if there:
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            # LeStash's own unfinished copy: hidden, so the Kobo never imports it. One
            # left by a run that was stopped is LeStash's too, and is replaced.
            part = dest.with_name(f".{dest.name}{PART}")
            try:
                shutil.copyfile(result.wanted.file, part)
                with part.open("rb+") as f:
                    os.fsync(f.fileno())
                if _sha(part) != result.wanted.sha256:
                    raise OSError("the copy read back from the Kobo doesn't match; removed")
                part.rename(dest)  # never over a file: _place found the name free
            except BaseException:
                part.unlink(missing_ok=True)  # stopped or failed: remove the unfinished copy
                raise
            result.made = True
        except OSError as e:
            result.path = None
            result.error = str(e)
    return results


def add_file_commands(app: typer.Typer, store_for: Callable[[Config], KoboStore]) -> None:
    """Register the PDF commands on the ``lestash kobo`` app."""

    def _switch(item_id: int, wanted: bool) -> None:
        with get_connection(Config.load()) as conn:
            if not conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                console.print(f"[red]Item {item_id} doesn't exist.[/red]")
                raise typer.Exit(1)
            set_wanted(conn, item_id, KOBO, wanted)
            conn.commit()
            console.print(f"Item {item_id}: {item_place(conn, item_id).text()}")

    @app.command("send")
    def send_cmd(item: Annotated[int, typer.Argument(help="The item's id")]) -> None:
        """Ask for an item on the Kobo on its own; it is copied at the next plug-in."""
        _switch(item, True)

    @app.command("unsend")
    def unsend_cmd(item: Annotated[int, typer.Argument(help="The item's id")]) -> None:
        """Stop asking for an item on the Kobo. A copy already there stays."""
        _switch(item, False)

    @app.command("files")
    def files_cmd() -> None:
        """List the items asked for on the Kobo, and where each stands."""
        with get_connection(Config.load()) as conn:
            ids = sorted(asked_for(conn, KOBO))
            if not ids:
                console.print("No item is asked for on the Kobo.")
                return
            for item_id in ids:
                place = item_place(conn, item_id)
                (title,) = conn.execute(
                    "SELECT title FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                via = "you" if place.marked else ", ".join(place.collections)
                console.print(f"{item_id}  {title}  ({via}): {place.text()}")

    @app.command("extract-notes")
    def notes_cmd() -> None:
        """Read the notes drawn on PDFs, from each annotated state not read yet.

        Run by the import unit after each plug-in's import.
        """
        from lestash_kobo.notes import extract_notes

        with get_connection(Config.load()) as conn:
            try:
                run = SyncRun.start(conn, NOTES_SOURCE)
            except SyncAlreadyRunning:
                console.print("[red]Notes are being read already.[/red]")
                raise typer.Exit(1) from None
            with run:
                result = extract_notes(conn, Config.load())
                for error in result.errors:
                    console.print(f"[red]{error}[/red]")
                run.finish(
                    "failed" if result.errors else "completed",
                    items_added=result.added,
                    items_updated=0,
                    error="; ".join(result.errors) or None,
                )
        console.print(
            f"Read {result.states} annotated states: {result.added} new notes"
            f" on {len(result.books)} books."
        )
        if result.errors:
            raise typer.Exit(1)

    @app.command("copy-files")
    def copy_cmd(
        root: Annotated[
            Path, typer.Option("--root", help="The Kobo mounted read-write")
        ] = RW_MOUNT,
        mount: Annotated[
            bool, typer.Option("--mount", help="Mount --root first, and unmount after")
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Say what would be copied; touch nothing")
        ] = False,
    ) -> None:
        """Copy the PDFs asked for onto the Kobo. Run by the plug-in unit after the write."""
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
        config = Config.load()
        store = store_for(config)
        with get_connection(config) as conn:
            wanted, problems = to_copy(conn, config, KOBO)
            for problem in problems:
                console.print(f"[yellow]{problem}[/yellow]")
            held = held_on_kobo(store)
            if not wanted:
                console.print("Nothing to copy to the Kobo.")
                return
            for w in wanted:
                there = held.get(w.sha256)
                console.print(f"{w.title}: " + (f"on the Kobo as {there}" if there else "to copy"))
            if dry_run:
                return
            try:
                run = SyncRun.start(conn, LOG_SOURCE)
            except SyncAlreadyRunning:
                console.print("[red]Another copy to the Kobo is running.[/red]")
                raise typer.Exit(1) from None
            with run:
                try:
                    with rw_mounted(root, mount):
                        results = copy_files(root, wanted, held)
                except OSError as e:
                    for w in wanted:
                        record_copy_error(conn, w.item_id, KOBO, str(e))
                    conn.commit()
                    run.finish("failed", items_added=0, items_updated=0, error=str(e))
                    console.print(f"[red]Nothing copied: {e}[/red]")
                    raise typer.Exit(1) from None
                when = now_text()
                errors = []
                for r in results:
                    if r.error or r.path is None:
                        record_copy_error(conn, r.wanted.item_id, KOBO, r.error or "not copied")
                        errors.append(f"item {r.wanted.item_id}: {r.error}")
                        console.print(f"[red]{r.wanted.title}: {r.error}[/red]")
                    else:
                        record_copy(conn, r.wanted.item_id, KOBO, r.path, r.wanted.sha256, when)
                        word = "copied to" if r.made else "already on the Kobo as"
                        console.print(f"{r.wanted.title}: {word} {r.path}")
                conn.commit()
                run.finish(
                    "failed" if errors else "completed",
                    items_added=sum(r.made for r in results),
                    items_updated=0,
                    error="; ".join(errors) or None,
                )
            if errors:
                raise typer.Exit(1)

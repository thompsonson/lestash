"""``lestash kobo push-jelu``: the Kobo's reading status into Jelu (#203).

Shows what it would change; ``--apply`` makes the changes. Not run by any timer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import httpx
import typer
from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.core.logging import get_plugin_logger
from lestash.core.sync import SyncAlreadyRunning, SyncRun
from rich.console import Console
from rich.markup import escape

from lestash_kobo.store import KoboStore

console = Console()
logger = get_plugin_logger("kobo")

SECTIONS = (
    ("create", "To create in Jelu, then mark"),
    ("start", "To mark as reading"),
    ("finish", "To mark as finished"),
    ("conflict", "Conflicts: Jelu's status is left as it is"),
    ("weak match", "Weak matches, by title only: not acted on"),
    ("rejected", "Pairs you rejected as not the same book"),
)


def add_jelu_commands(app: typer.Typer, store_for: Callable[[Config], KoboStore]) -> None:
    @app.command("push-jelu")
    def push_jelu_cmd(
        apply_changes: Annotated[
            bool, typer.Option("--apply", help="Make the changes; without it, only show them")
        ] = False,
        titles: Annotated[
            list[int] | None,
            typer.Option(
                "--title",
                help="A Kobo item's id, pushed though it isn't a book (no ISBN); repeatable",
            ),
        ] = None,
    ) -> None:
        """Send the Kobo's reading and finished books to Jelu as reading events.

        Reads the newest snapshot and your Jelu library. A status only moves forward;
        nothing in Jelu is changed in place or deleted.
        """
        from lestash_jelu.client import JeluAuthError, JeluClient, load_credentials

        from lestash_kobo import jelu_push as push
        from lestash_kobo.reader import KoboReader

        config = Config.load()
        store = store_for(config)
        snapshot = store.latest()
        if snapshot is None:
            console.print("[red]No Kobo snapshot yet; plug the Kobo in first.[/red]")
            raise typer.Exit(1)
        creds = load_credentials()
        if not creds:
            console.print("[red]Jelu isn't set up. Run: lestash jelu auth --url ... -u ...[/red]")
            raise typer.Exit(1)

        with get_connection(config) as conn:
            also: set[str] = set()
            for item_id in titles or []:
                row = conn.execute(
                    "SELECT source_id FROM items WHERE id = ? AND source_type = ?",
                    (item_id, push.SOURCE_TYPE),
                ).fetchone()
                if row is None:
                    console.print(f"[red]Item {item_id} isn't a Kobo book.[/red]")
                    raise typer.Exit(1)
                also.add(row[0])

            kobo = push.kobo_titles(KoboReader(store, snapshot).title_rows())
            client = JeluClient(
                creds["url"], creds["username"], creds["password"], creds.get("token")
            )
            try:
                with client:
                    books = push.jelu_books(client.userbooks(), client.books())
                    todo = push.plan(conn, kobo, books, also)
                    _show(todo, snapshot.name, apply_changes)
                    if not apply_changes or not todo.changes:
                        _remember(creds, client)
                        return
                    try:
                        run = SyncRun.start(conn, push.LOG_SOURCE)
                    except SyncAlreadyRunning:
                        console.print("[red]A push to Jelu is running already.[/red]")
                        raise typer.Exit(1) from None
                    with run:
                        created, events, links, errors = push.apply(conn, client, todo)
                        for step in todo.changes:
                            logger.info("push-jelu: %s", step.text())
                        run.finish(
                            "failed" if errors else "completed",
                            items_added=created,
                            items_updated=events,
                            error="; ".join(errors) or None,
                        )
                    _remember(creds, client)
            except (JeluAuthError, httpx.HTTPError) as e:
                console.print(f"[red]Jelu request failed: {escape(str(e))}[/red]")
                raise typer.Exit(1) from e
        for error in errors:
            console.print(f"[red]{escape(error)}[/red]")
        console.print(
            f"Done: {created} book(s) created, {events} status(es) sent, {links} link(s) added."
        )
        if errors:
            raise typer.Exit(1)


def _remember(creds: dict[str, str], client) -> None:
    from lestash_jelu.client import save_credentials

    if client.token and client.token != creds.get("token"):
        save_credentials(creds["url"], creds["username"], creds["password"], client.token)


def _show(todo, snapshot: str, applying: bool) -> None:
    from lestash_kobo import jelu_push as push

    console.print(f"Kobo snapshot {snapshot}.")
    for action, heading in SECTIONS:
        steps = todo.of(action)
        if steps:
            console.print(f"\n[bold]{heading}[/bold] ({len(steps)})")
            for step in steps:
                console.print(f"  {escape(step.text())}")
    in_step = todo.of(push.NOTHING)
    left = todo.of(push.LEFT_OUT)
    console.print(f"\nAlready in step with Jelu: {len(in_step)}.")
    if left:
        console.print(
            f"Left out, not books (no ISBN, not in Jelu): {len(left)}. "
            "Push one with --title <item id>:"
        )
        for step in left:
            item = step.kobo.item_id if step.kobo.item_id is not None else "not imported"
            console.print(f"  {item}  {escape(step.text())} ({step.kobo.status})")
    if todo.changes and not applying:
        console.print(f"\n{len(todo.changes)} change(s). Nothing was sent; run with --apply.")
    elif not todo.changes:
        console.print("\nNothing to send.")

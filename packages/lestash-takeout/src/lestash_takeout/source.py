"""Google Takeout source plugin.

Set Takeout's delivery method to "Add to Drive" (optionally "export every 2 months"). Each sync
lists your Takeout zips in Drive, imports the ones not done yet with LeStash's zip importer
(Keep, Gemini, NotebookLM, YouTube history, ...), and deletes the downloaded copy. Uses
LeStash's Google sign-in with read-only Drive access; nothing is changed in Drive.

Which zips are done is kept in the ``sync_checkpoints`` table (source ``takeout``, key = Drive
file id). The plugin yields a checkpoint after a zip's items, and the sync loop commits both
together, so a zip is never marked done while its items are missing.

A zip that fails to download or read is recorded as ``failed`` with its attempt count, and the
sync then ends ``failed``. It is retried up to ``MAX_ATTEMPTS`` times, then left alone until
``lestash takeout forget`` or a new importer version.
"""

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.importers import IMPORTER_VERSION, CorruptZip, parse_zip
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_takeout.drive import download, list_takeout_zips

console = Console()
logger = get_plugin_logger("takeout")

SOURCE = "takeout"
MAX_ATTEMPTS = 3  # a zip that fails this often is not downloaded again


class TakeoutError(Exception):
    """One or more zips failed; raised after the others are imported."""


def _drive_service() -> Any:
    from lestash.core.google_auth import get_drive_service

    return get_drive_service()


def load_state() -> dict[str, dict[str, Any]]:
    """Zips already handled, by Drive file id."""
    from lestash.core.config import Config
    from lestash.core.database import get_checkpoints, get_connection

    with get_connection(Config.load()) as conn:
        return get_checkpoints(conn, SOURCE)


def forget(file_id: str) -> bool:
    """Drop a zip's record so the next sync imports it again."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection, save_checkpoint

    with get_connection(Config.load()) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sync_checkpoints WHERE source = ? AND key = ?", (SOURCE, file_id)
        ).fetchone()
        save_checkpoint(conn, SOURCE, file_id, None)
        conn.commit()
    return exists is not None


def attempts(record: dict[str, Any] | None) -> int:
    """Failed attempts so far with this importer version; a newer importer starts again."""
    if not record or record.get("status") != "failed":
        return 0
    if record.get("importer_version", 1) < IMPORTER_VERSION:
        return 0
    return int(record.get("attempts", 1))


def is_done(record: dict[str, Any] | None) -> bool:
    """A zip is done once imported, or once it has failed ``MAX_ATTEMPTS`` times.

    One with nothing recognisable, or that kept failing, is retried by a newer importer,
    which may know its format (e.g. YouTube history, added in version 2).
    """
    if not record:
        return False
    if record.get("status") == "unrecognized":
        return record.get("importer_version", 1) >= IMPORTER_VERSION
    if record.get("status") == "failed":
        return attempts(record) >= MAX_ATTEMPTS
    return True


def _record(file: dict[str, Any], status: str, items: int, source_type: str | None) -> dict:
    return {
        "name": file["name"],
        "status": status,
        "items": items,
        "source_type": source_type,
        "importer_version": IMPORTER_VERSION,
        "imported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def failure(file: dict[str, Any], previous: dict[str, Any] | None, error: Exception) -> dict:
    """The record of a failed attempt: counts it, and says whether it will be retried."""
    record = _record(file, "failed", 0, None)
    record["attempts"] = attempts(previous) + 1
    record["error"] = str(error)[:500] or type(error).__name__
    return record


def describe_failure(record: dict[str, Any]) -> str:
    n = attempts(record)
    retry = "gave up; `lestash takeout forget` retries it" if n >= MAX_ATTEMPTS else "will retry"
    return f"failed {n} of {MAX_ATTEMPTS} times ({retry}): {record.get('error', '')}"


def import_zip(
    service: Any,
    file: dict[str, Any],
    workdir: Path | None = None,
    previous: dict[str, Any] | None = None,
) -> Iterator[ItemCreate | SyncCheckpoint]:
    """Download one Takeout zip and yield its items, then its checkpoint.

    The download is deleted before the items are yielded. A download or read error, or a
    corrupt (e.g. truncated) zip, yields a ``failed`` checkpoint counting the attempt;
    ``previous`` is the zip's stored record.
    """
    try:
        with tempfile.TemporaryDirectory(dir=workdir) as tmp:
            path = download(service, file["id"], Path(tmp) / file["name"])
            try:
                items, source_type = parse_zip(path)
            except CorruptZip:
                raise  # a bad download, not a format we don't know
            except ValueError as e:
                # Nothing this importer understands (e.g. only photos): record it, so it is
                # retried only once the importer version changes.
                logger.info(f"{file['name']}: {e}")
                yield SyncCheckpoint(key=file["id"], value=_record(file, "unrecognized", 0, None))
                return
    except Exception as e:  # noqa: BLE001 - one bad zip must not stop the others
        record = failure(file, previous, e)
        logger.warning(f"{file['name']} ({file['id']}): {describe_failure(record)}")
        yield SyncCheckpoint(key=file["id"], value=record)
        return
    yield from items
    yield SyncCheckpoint(key=file["id"], value=_record(file, "imported", len(items), source_type))
    logger.info(f"{file['name']}: {len(items)} items ({source_type})")


def pending_zips(service: Any, state: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pending = []
    for f in list_takeout_zips(service):
        record = state.get(f["id"])
        if not is_done(record):
            pending.append(f)
        elif record and record.get("status") == "failed":
            logger.warning(f"{f['name']} ({f['id']}): {describe_failure(record)}")
    return pending


class TakeoutSource(SourcePlugin):
    """Google Takeout (delivered to Drive) source plugin."""

    name = SOURCE
    description = "Google Takeout zips delivered to Drive"

    def get_commands(self) -> typer.Typer:
        app = typer.Typer(help="Google Takeout (delivered to Drive) commands.")

        @app.command("list")
        def list_cmd() -> None:
            """Show Takeout zips in Drive and whether they've been imported."""
            state = load_state()
            table = Table(title="Takeout zips in Drive")
            for col in ("Name", "Size", "Created", "Status", "ID"):
                table.add_column(col)
            for f in list_takeout_zips(_drive_service()):
                seen = state.get(f["id"])
                if not seen:
                    status = "pending"
                elif seen.get("status") == "unrecognized":
                    status = "nothing to import" + ("" if is_done(seen) else " (will retry)")
                elif seen.get("status") == "failed":
                    status = describe_failure(seen)
                else:
                    status = f"{seen['items']} items, {seen['imported_at'][:10]}"
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
            """Drop a zip's record (imported, failed, ...), so the next sync imports it again."""
            console.print("Forgotten." if forget(file_id) else "That id had no record.")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate | SyncCheckpoint]:
        try:
            service = _drive_service()
        except ValueError as e:
            logger.warning(f"Google Drive not available: {e}")
            return
        state = load_state()
        failed: list[str] = []
        for file in pending_zips(service, state):
            for obj in import_zip(service, file, previous=state.get(file["id"])):
                if isinstance(obj, SyncCheckpoint) and (obj.value or {}).get("status") == "failed":
                    failed.append(f"{file['name']} ({describe_failure(obj.value or {})})")
                yield obj
        if failed:
            # After the other zips' items and checkpoints, so the sync is logged failed
            raise TakeoutError(f"{len(failed)} zip(s) failed: " + "; ".join(failed))

    def configure(self) -> dict:
        return {}

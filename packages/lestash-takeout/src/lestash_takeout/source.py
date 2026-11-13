"""Google Takeout source plugin.

Set Takeout's delivery method to "Add to Drive" (optionally "export every 2 months"). Each sync
lists your Takeout zips in Drive, imports the ones not done yet with LeStash's zip importer
(Keep, Gemini, NotebookLM, YouTube history, ...), and deletes the downloaded copy. Uses
LeStash's Google sign-in with read-only Drive access; nothing is changed in Drive.

Which zips are done is kept in the ``sync_checkpoints`` table (source ``takeout``, key = Drive
file id). The plugin yields a checkpoint after a zip's items, and the sync loop commits both
together, so a zip is never marked done while its items are missing.

NotebookLM's generated artifacts (slides, audio, video…) spread their files over an export's
parts. Each part's artifact files go into the file store as it is read, and what the export's
parts held so far is kept as a checkpoint (key ``notebooklm:takeout-<request time>``). Each part
then yields the export's artifacts again, with every file seen so far. Once all its parts are
settled, the export is completed from that index, with no zip needed.

A zip that can't be read, or that Drive says is gone (404, 403), is recorded as ``failed``
with its attempt count, and the sync then ends ``failed``. It is retried up to
``MAX_ATTEMPTS`` times, then left alone until ``lestash takeout forget`` or a new importer
version. Any other download error (sign-in, network, a Drive 5xx or rate limit, a full disk)
would fail every zip the same way: the sync stops there and records nothing for that zip.
"""

import lzma
import re
import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from lestash.core.logging import get_plugin_logger
from lestash.importers import IMPORTER_VERSION, CorruptZip, parse_zip
from lestash.models.item import ItemCreate
from lestash.models.sync import SyncCheckpoint
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_takeout.drive import download, list_takeout_zips

if TYPE_CHECKING:
    from lestash.core.config import Config

console = Console()
logger = get_plugin_logger("takeout")

SOURCE = "takeout"
MAX_ATTEMPTS = 3  # a zip that fails this often is not downloaded again


class TakeoutError(Exception):
    """One or more zips failed; raised after the others are imported."""


def _drive_service() -> Any:
    from lestash.core.google_auth import get_drive_service

    return get_drive_service()


def load_state(app_config: "Config | None" = None) -> dict[str, dict[str, Any]]:
    """Zips already handled, by Drive file id, in ``app_config``'s database (else the
    default's)."""
    from lestash.core.config import Config
    from lestash.core.database import get_checkpoints, get_connection

    with get_connection(app_config or Config.load()) as conn:
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


# Takeout uploads a big export's parts to Drive over a while: until its newest part is this
# old, another may still come, so the export is not treated as complete
UPLOAD_GRACE = timedelta(hours=12)


def uploads_done(parts: list[dict[str, Any]], now: datetime | None = None) -> bool:
    """Whether an export's listed parts are old enough that no more are coming."""
    now = now or datetime.now(UTC)
    for f in parts:
        try:
            created = datetime.fromisoformat(str(f.get("createdTime", "")).replace("Z", "+00:00"))
        except ValueError:
            continue  # unknown: no reason to wait
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now - created < UPLOAD_GRACE:
            return False
    return True


def settled(record: dict[str, Any] | None) -> bool:
    """A part whose artifacts the export's index has, or never will: read by this importer
    (``indexed``), or given up. A part imported before artifacts were indexed is not."""
    if not is_done(record) or record is None:
        return False
    return bool(record.get("indexed")) or record.get("status") == "failed"


def _record(file: dict[str, Any], status: str, items: int, source_type: str | None) -> dict:
    return {
        "indexed": status in ("imported", "unrecognized"),  # its NotebookLM artifacts read
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


# 403 reasons that clear by themselves: rate limits (user/rateLimitExceeded) and quotas
# (downloadQuotaExceeded, a file downloaded too often today; dailyLimitExceeded).
TRANSIENT_403 = (b"ratelimitexceeded", b"quotaexceeded", b"dailylimitexceeded")


def _file_is_unavailable(error: Exception) -> bool:
    """Whether a download error is about this file (gone or not shared with us), so retrying
    it counts as an attempt. Anything else is about the sign-in, network, Drive or disk."""
    from googleapiclient.errors import HttpError

    if not isinstance(error, HttpError):
        return False
    status = int(error.resp.status)
    body = (error.content or b"").lower()
    if status == 403 and any(reason in body for reason in TRANSIENT_403):
        return False  # Drive reports rate limits and daily quotas as 403
    return status in (403, 404)


REQUEST = re.compile(r"^(takeout-\d{8}T\d{6}Z)-")


def export_key(file: dict[str, Any]) -> str:
    """The checkpoint key of the Takeout request a part belongs to. One request's parts
    are ``takeout-<time>-001.zip``…, and ``takeout-<time>-3-001.zip``… when Takeout
    splits it further: all are ``notebooklm:takeout-<time>``."""
    name = file["name"]
    if m := REQUEST.match(name):
        return "notebooklm:" + m.group(1)
    return "notebooklm:" + re.sub(r"-\d+\.zip$", "", name)


def _artifacts(
    path: Path, kept: dict[str, Any] | None, final: bool, app_config: "Config | None" = None
) -> tuple[list[ItemCreate], Any]:
    """The export's NotebookLM artifacts with this part read, and its index to keep; no
    items and None if the part holds no artifact files. ``final``: no other part of the
    export is still to be read. A file store that can't be written stops the sync.
    ``app_config``: the database whose file store takes the files (else the default)."""
    from lestash.core.blobs import BlobStore, store_root
    from lestash.core.config import Config
    from lestash.importers.notebooklm_artifacts import Index, index_zip

    try:
        fresh = index_zip(path, BlobStore(store_root(app_config or Config.load())))
    except (
        zipfile.BadZipFile,
        KeyError,
        zlib.error,
        lzma.LZMAError,
        EOFError,
        NotImplementedError,
        RuntimeError,  # an encrypted member
    ) as e:
        raise CorruptZip(f"{type(e).__name__}: {e}") from e  # the part: counts an attempt
    except OSError as e:
        # The store's disk, not this zip: every zip would fail the same way
        raise TakeoutError(f"stopped, file store not writable: {type(e).__name__}: {e}") from e
    if not fresh.descriptions and not fresh.files and not final:
        return [], None
    index = Index.from_json(kept)
    index.descriptions.update(fresh.descriptions)
    index.files.update(fresh.files)
    if not index.descriptions and not index.files:
        return [], None
    return _complete(index, final)


def _complete(index: Any, final: bool) -> tuple[list[ItemCreate], dict[str, Any]]:
    from lestash.importers.notebooklm_artifacts import collect, to_items

    artifacts, unmatched = collect(index, final=final)
    if unmatched:
        verb = "have no description" if final else "wait for their description"
        logger.info(f"{len(unmatched)} NotebookLM file(s) {verb}")
    return to_items(artifacts), {**index.to_json(), "final": final}


def finish_exports(
    listed: list[dict[str, Any]], state: dict[str, dict[str, Any]]
) -> Iterator[ItemCreate | SyncCheckpoint]:
    """Complete each export whose parts are all settled but whose last read was not
    final: its last part was given up, or a listed part left Drive. Its files are in
    the store already, so no zip is read."""
    from lestash.importers.notebooklm_artifacts import Index

    parts: dict[str, list[dict[str, Any]]] = {}
    for f in listed:
        parts.setdefault(export_key(f), []).append(f)
    exports = {k for k in state if k.startswith("notebooklm:")}
    for export in sorted(exports):
        kept = state[export]
        listed_parts = parts.get(export, [])
        if (
            kept.get("final")
            or not uploads_done(listed_parts)
            or not all(settled(state.get(f["id"])) for f in listed_parts)
        ):
            continue
        items, value = _complete(Index.from_json(kept), final=True)
        yield from items
        state[export] = value
        yield SyncCheckpoint(key=export, value=value)


def import_zip(
    service: Any,
    file: dict[str, Any],
    workdir: Path | None = None,
    previous: dict[str, Any] | None = None,
    kept_index: dict[str, Any] | None = None,
    final: bool = True,
    app_config: "Config | None" = None,
) -> Iterator[ItemCreate | SyncCheckpoint]:
    """Download one Takeout zip and yield its items, then its checkpoint.

    Its export's NotebookLM artifacts come after its items, with every file the export's
    parts held so far, then the export's index (``kept_index`` is the stored one).
    ``final``: no other part of the export is still to be read (see collect).
    ``app_config``: the database being synced into, for its file store (else the default).

    The download is deleted before the items are yielded. A corrupt (e.g. truncated) zip, a
    parse error, or a file Drive says is gone yields a ``failed`` checkpoint counting the
    attempt; ``previous`` is the zip's stored record. Any other download error raises
    ``TakeoutError`` and records nothing: it would fail the next zip too.
    """
    error: Exception | None = None
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        try:
            path = download(service, file["id"], Path(tmp) / file["name"])
        except Exception as e:
            if not _file_is_unavailable(e):
                raise TakeoutError(
                    f"stopped at {file['name']}, nothing recorded for it: {type(e).__name__}: {e}"
                ) from e
            error = e
        else:
            unrecognized = False
            try:
                items, source_type = parse_zip(path)
            except CorruptZip as e:
                error = e  # a bad download, not a format we don't know
            except ValueError as e:
                # Nothing this importer understands (e.g. only photos): record it, so it is
                # retried only once the importer version changes.
                logger.info(f"{file['name']}: {e}")
                unrecognized, items, source_type = True, [], None
            except Exception as e:  # noqa: BLE001 - one bad zip must not stop the others
                error = e
            if error is None:
                try:
                    # Also for a part with nothing else: as the export's last, it completes
                    # the artifacts the other parts left waiting
                    artifact_items, index = _artifacts(path, kept_index, final, app_config)
                except CorruptZip as e:
                    error = e
    if error is not None:
        record = failure(file, previous, error)
        logger.warning(f"{file['name']} ({file['id']}): {describe_failure(record)}")
        yield SyncCheckpoint(key=file["id"], value=record)
        return
    yield from items
    yield from artifact_items
    if index is not None:
        yield SyncCheckpoint(key=export_key(file), value=index)
    if unrecognized:
        yield SyncCheckpoint(key=file["id"], value=_record(file, "unrecognized", 0, None))
        return
    record = _record(file, "imported", len(items), source_type)
    if artifact_items:
        record["artifacts"] = len(artifact_items)  # the export's, so far
    yield SyncCheckpoint(key=file["id"], value=record)
    logger.info(f"{file['name']}: {len(items)} items, {len(artifact_items)} artifacts")


# Free space to leave on the download disk besides the zip itself.
DISK_HEADROOM = 512 * 1024 * 1024


def lacks_space(file: dict[str, Any], workdir: Path | None = None) -> str | None:
    """Why the zip won't fit on the download disk, or None if it will (or its size is unknown).

    Checked before downloading, so one big part doesn't fill the disk on every sync and hold
    up the smaller zips after it."""
    size = int(file.get("size") or 0)
    free = shutil.disk_usage(workdir or tempfile.gettempdir()).free
    if size and size + DISK_HEADROOM > free:
        return f"needs {size / 1e9:.1f} GB free to download, {free / 1e9:.1f} GB free; will retry"
    return None


def pending_zips(
    service: Any, state: dict[str, dict[str, Any]], listed: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    pending = []
    for f in list_takeout_zips(service) if listed is None else listed:
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

        @app.command("dedupe-notebooks")
        def dedupe_notebooks_cmd(
            apply: Annotated[
                bool, typer.Option("--apply", help="Merge them; without it, only list them")
            ] = False,
        ) -> None:
            """Merge NotebookLM notebooks held twice under two folder names, with their
            notes, chats and artifacts. Lists them unless --apply is given."""
            from lestash.core.database import get_connection
            from lestash.importers.notebooklm_dedupe import dedupe_notebooklm, find_duplicates

            with get_connection(self.app_config) as conn:
                if apply:
                    pairs = dedupe_notebooklm(conn)
                    conn.commit()
                else:
                    pairs = find_duplicates(conn)
            for pair in pairs:
                console.print(f"  {pair.old_id} → {pair.current_id}  {pair.title}")
            verb = "Merged" if apply else "Would merge"
            console.print(f"{verb} {len(pairs)} notebook copies into their current items.")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate | SyncCheckpoint]:
        try:
            service = _drive_service()
        except ValueError as e:
            logger.warning(f"Google Drive not available: {e}")
            return
        app_config = self.app_config
        state = load_state(app_config)
        problems: list[str] = []
        try:
            listed = list_takeout_zips(service)
            pending = pending_zips(service, state, listed)
            for file in pending:
                if reason := lacks_space(file):
                    # Not the zip's fault: no attempt counted, and smaller zips still import
                    logger.warning(f"{file['name']} ({file['id']}): {reason}")
                    problems.append(f"{file['name']} ({reason})")
                    continue
                export = export_key(file)
                # The last of its export's parts still to read: the others are done, and
                # no more are still being uploaded
                siblings = [f for f in listed if export_key(f) == export]
                final = uploads_done(siblings) and all(
                    settled(state.get(f["id"])) for f in siblings if f["id"] != file["id"]
                )
                parts = import_zip(
                    service,
                    file,
                    previous=state.get(file["id"]),
                    kept_index=state.get(export),
                    final=final,
                    app_config=app_config,
                )
                for obj in parts:
                    if isinstance(obj, SyncCheckpoint) and obj.key in (export, file["id"]):
                        state[obj.key] = obj.value or {}  # the export's next part reads it
                    if (
                        isinstance(obj, SyncCheckpoint)
                        and (obj.value or {}).get("status") == "failed"
                    ):
                        problems.append(f"{file['name']} ({describe_failure(obj.value or {})})")
                    yield obj
            yield from finish_exports(listed, state)
        except TakeoutError as e:  # sign-in, network, Drive or disk: the rest would fail too
            logger.warning(f"Takeout sync stopped: {e}")
            problems.insert(0, str(e))
        if problems:
            # After the other zips' items and checkpoints, so the sync is logged failed
            raise TakeoutError("; ".join(problems))

    def configure(self) -> dict:
        return {}

"""Source API endpoints."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, HTTPException
from lestash.core import import_reminders as reminders
from lestash.core.config import Config
from lestash.core.database import get_connection
from lestash.core.sync import SyncAlreadyRunning, SyncRun, SyncStore
from lestash.plugins.loader import load_plugins

from lestash_server.deps import get_config, get_db
from lestash_server.models import (
    FailingSyncEntry,
    ImportAimRequest,
    ImportReminder,
    ImportRemindersResponse,
    ImportSnoozeRequest,
    SourceResponse,
    SyncLogEntry,
)

logger = logging.getLogger(__name__)

START_WAIT_S = 30  # how long a sync request waits for the sync to begin

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("", response_model=list[SourceResponse])
def list_sources():
    """List installed source plugins with sync status."""
    plugins = load_plugins()
    results = []

    with get_db() as conn:
        for name, plugin in plugins.items():
            row = conn.execute(
                "SELECT enabled, last_sync FROM sources WHERE source_type = ?",
                (name,),
            ).fetchone()

            results.append(
                SourceResponse(
                    name=name,
                    description=plugin.description,
                    enabled=bool(row["enabled"]) if row else True,
                    last_sync=row["last_sync"] if row else None,
                )
            )

    return results


@router.get("/status", response_model=list[SyncLogEntry])
def source_status(source: str | None = None):
    """Show recent sync history: the 20 newest runs, of one source if given."""
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT id, source_type, started_at, completed_at, status,
                   items_added, items_updated, error_message
            FROM sync_log
            {"WHERE source_type = ?" if source else ""}
            ORDER BY started_at DESC
            LIMIT 20
            """,
            (source,) if source else (),
        ).fetchall()

    return [SyncLogEntry(**dict(row)) for row in rows]


def _iso(when: datetime | None) -> str | None:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None


def _reminders(conn) -> ImportRemindersResponse:
    return ImportRemindersResponse(
        imports=[
            ImportReminder(
                key=s.source.key,
                label=s.source.label,
                note=s.source.note,
                how=s.source.how,
                sync=s.source.checkpoint_source if s.source.how == "drive" else None,
                export_url=s.source.export_url,
                last_import=_iso(s.last_import),
                days=s.days,
                aim_days=s.aim_days,
                default_aim_days=s.source.aim_days,
                snoozed_until=_iso(s.snoozed_until),
                due=s.due,
            )
            for s in reminders.statuses(conn)
        ],
        failing=[
            FailingSyncEntry(
                source=f.source,
                failed_runs=f.failed_runs,
                last_error=f.last_error,
                last_ok=_iso(f.last_ok),
            )
            for f in reminders.failing_syncs(conn)
        ],
    )


@router.get("/imports", response_model=ImportRemindersResponse)
def import_reminders():
    """Imports you run yourself, most overdue first, and syncs that keep failing (#403)."""
    with get_db() as conn:
        return _reminders(conn)


@router.put("/imports/{key}", response_model=ImportRemindersResponse)
def set_import_aim(key: str, body: ImportAimRequest):
    """Set how often you mean to run an import, in days; 0 never reminds, null resets."""
    with get_db() as conn:
        try:
            reminders.set_aim(conn, key, body.aim_days)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown import: {key}") from None
        conn.commit()
        return _reminders(conn)


@router.post("/imports/{key}/snooze", response_model=ImportRemindersResponse)
def snooze_import(key: str, body: ImportSnoozeRequest | None = None):
    """Hold an import's reminder for some days (7 unless given); 0 ends the snooze."""
    with get_db() as conn:
        try:
            reminders.snooze(conn, key, body.days if body else reminders.DEFAULT_SNOOZE_DAYS)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Unknown import: {key}") from None
        conn.commit()
        return _reminders(conn)


# Told when a sync begins: ("started", its sync_log id); ("already_running", None)
# when another sync holds the lock (its row may not be written yet, so it isn't
# named); or ("skipped", None) if it never got that far
OnStart = Callable[[str, int | None], None]


def _run_sync(source_name: str, on_start: OnStart | None = None) -> None:
    """Run a sync in the background."""
    told = False

    def tell(status: str, run_id: int | None) -> None:
        nonlocal told
        if on_start is not None and not told:
            told = True
            on_start(status, run_id)

    try:
        _sync(source_name, tell)
    finally:
        tell("skipped", None)  # never left waiting, whatever happened


def _sync(source_name: str, tell: OnStart) -> None:
    plugins = load_plugins()
    plugin = plugins.get(source_name)
    if not plugin:
        return

    config = get_config()
    # Read the file again, not the copy cached at startup, so a `lestash config set`
    # made while the server runs takes effect at the next sync. A file that can't be
    # read (e.g. mid-edit) falls back to the cached copy rather than lose the sync.
    try:
        plugin_config = Config.load().get_plugin_config(source_name)
    except Exception as e:
        logger.warning("Could not read the config file (%s); using the one loaded at startup", e)
        plugin_config = config.get_plugin_config(source_name)

    plugin.app_config = config  # the plugin's own reads use this database too
    with get_connection(config) as conn:
        store = SyncStore(conn, source_name)
        try:
            run = SyncRun.start(conn, source_name, store)
        except SyncAlreadyRunning as e:
            logger.warning(f"Sync skipped for {source_name}: {e}")
            tell("already_running", None)
            return
        tell("started", run.id)

        with run:
            error_message = None
            try:
                store.run(plugin.sync(plugin_config))
                conn.execute(
                    """
                    INSERT INTO sources (source_type, last_sync)
                    VALUES (?, ?)
                    ON CONFLICT(source_type) DO UPDATE SET last_sync = excluded.last_sync
                    """,
                    (source_name, datetime.now()),
                )
                conn.commit()
                status = "completed"
            except Exception as e:
                status = "failed"
                error_message = str(e)
                logger.error(f"Sync failed for {source_name}: {e}")
            run.finish(status, error=error_message)


@router.post("/{source_name}/sync")
def sync_source(source_name: str):
    """Start a sync for a source plugin; returns once it has begun.

    ``status``: ``started``, with ``run_id``, its sync_log id; ``already_running`` when
    another sync of the source holds its lock; ``pending`` if it hadn't begun within
    START_WAIT_S; ``skipped`` if it stopped before it began.
    """
    plugins = load_plugins()
    if source_name not in plugins:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name}")

    # The sync runs in its own thread; the reply waits only until it has begun, so it
    # can name the run to watch: this one, or one already running (#403)
    began: dict[str, object] = {"status": "pending", "run_id": None}  # not begun in time
    ready = threading.Event()

    def note(status: str, run_id: int | None) -> None:
        began.update(status=status, run_id=run_id)
        ready.set()

    # Not a daemon: a server stopping waits for the sync to finish and log its end,
    # as it did for the background task this replaces
    threading.Thread(target=_run_sync, args=(source_name, note), name=f"sync-{source_name}").start()
    ready.wait(START_WAIT_S)
    return {"status": began["status"], "source": source_name, "run_id": began["run_id"]}

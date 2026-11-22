"""Import reminders (#403): how long since you last ran each import LeStash can't fetch.

Some sources arrive only when you ask for an export and bring it in: a Google Takeout,
a Mistral export, a LinkedIn data export, a Claude export. Each is a ``ManualSource``.

- **Last import** is the latest of three signs: an ``imported`` activity, which every
  import writes from now on; the newest item first fetched from the source; and, for
  Takeout, the newest zip its Drive sync has handled. The last two cover imports made
  before imports were logged.
- **Aim** is how often you mean to import, in days; ``import_reminders`` keeps your own
  aim and a snooze. An aim of 0 means never remind.
- **Due** means past the aim and not snoozed. One LeStash can't read yet is never
  due: there would be nothing you could do about it.

A source that syncs by itself needs no reminder, but one whose recent syncs all failed
is listed too (``failing_syncs``), so a sync that stops is noticed.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

IMPORTED: Final = "imported"  # the activity verb
DEFAULT_SNOOZE_DAYS: Final = 7
FAILED_RUNS: Final = 3  # this many failed syncs in a row lists a source as failing


@dataclass(frozen=True)
class ManualSource:
    key: str
    label: str
    aim_days: int  # the default; 0 means never remind
    # Items first fetched from these sources count as an import of this one
    item_sources: tuple[str, ...] = ()
    # The newest of this source's sync checkpoints counts too (one per zip handled)
    checkpoint_source: str | None = None
    # Where to ask for the export
    export_url: str | None = None
    # How it comes in: "upload" (Sources → Import), "drive" (read from Drive by the
    # sync, or uploaded), a CLI command, or None when LeStash can't read it yet
    how: str | None = "upload"
    note: str | None = None


MANUAL_SOURCES: Final[tuple[ManualSource, ...]] = (
    ManualSource(
        "google-takeout",
        "Google Takeout",
        60,
        item_sources=("gemini", "notebooklm", "google-keep"),
        checkpoint_source="takeout",
        export_url="https://takeout.google.com",
        how="drive",
        note="Gemini, NotebookLM, Keep and YouTube history",
    ),
    ManualSource(
        "mistral",
        "Mistral export",
        30,
        item_sources=("mistral",),
        export_url="https://chat.mistral.ai",
    ),
    ManualSource(
        "linkedin-export",
        "LinkedIn data export",
        90,
        export_url="https://www.linkedin.com/mypreferences/d/download-my-data",
        how="lestash linkedin import <zip>",
        note="Older history the sync can't reach",
    ),
    ManualSource(
        "claude",
        "Claude export",
        30,
        item_sources=("claude", "claude-project", "claude-memory"),
        export_url="https://claude.ai/settings/data-privacy-controls",
        how=None,
        note="LeStash can't read this export yet (#88)",
    ),
)
BY_KEY: Final[Mapping[str, ManualSource]] = {s.key: s for s in MANUAL_SOURCES}

# What an importer reports it read, and the manual source that import counts for
IMPORT_KINDS: Final[Mapping[str, str]] = {
    "takeout": "google-takeout",
    "gemini": "google-takeout",
    "notebooklm": "google-takeout",
    "google-keep": "google-takeout",
    "youtube": "google-takeout",  # a zip with YouTube history is a Takeout
    "mistral": "mistral",
    "linkedin-export": "linkedin-export",
}


def _utc_text(when: datetime) -> str:
    """The form activities are stored in: UTC, 'YYYY-MM-DDTHH:MM:SS'."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _parse(text: str | None) -> datetime | None:
    """A stored time as UTC; one with no zone is taken as UTC."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def record_import(
    conn: sqlite3.Connection,
    kind: str,
    items_added: int,
    *,
    at: datetime | None = None,
    name: str | None = None,
) -> bool:
    """Log an import of ``kind`` as an ``imported`` activity, even one adding nothing.

    ``kind`` is what the importer reports (``takeout``, ``mistral``...). Kinds that are
    not a manual source are not logged. Returns whether it was. Doesn't commit.
    """
    key = IMPORT_KINDS.get(kind)
    if key is None:
        return False
    # The label as it was, so the Timeline can name an import from any day
    details: dict[str, object] = {"source": key, "label": BY_KEY[key].label}
    details["items_added"] = items_added
    if name:
        details["name"] = name
    conn.execute(
        "INSERT OR IGNORE INTO activities (verb, item_id, occurred_at, source, details)"
        " VALUES (?, NULL, ?, ?, ?)",
        (IMPORTED, _utc_text(at or datetime.now(UTC)), f"import:{key}", json.dumps(details)),
    )
    return True


def last_import(conn: sqlite3.Connection, source: ManualSource) -> datetime | None:
    """When ``source`` was last imported, by the latest of its signs."""
    found: list[datetime] = []
    row = conn.execute(
        "SELECT max(occurred_at) FROM activities WHERE verb = ? AND source = ?",
        (IMPORTED, f"import:{source.key}"),
    ).fetchone()
    if (when := _parse(row[0])) is not None:
        found.append(when)
    if source.item_sources:
        marks = ", ".join("?" for _ in source.item_sources)
        row = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%S', max(julianday(fetched_at)))"
            f" FROM items WHERE source_type IN ({marks})",
            source.item_sources,
        ).fetchone()
        if (when := _parse(row[0])) is not None:
            found.append(when)
    if source.checkpoint_source:
        row = conn.execute(
            "SELECT max(updated_at) FROM sync_checkpoints WHERE source = ?",
            (source.checkpoint_source,),
        ).fetchone()
        if (when := _parse(row[0])) is not None:
            found.append(when)
    return max(found) if found else None


@dataclass(frozen=True)
class ImportStatus:
    source: ManualSource
    last_import: datetime | None
    days: int | None  # whole days since, None if never
    aim_days: int  # yours, else the default; 0 means never remind
    snoozed_until: datetime | None
    due: bool


def statuses(conn: sqlite3.Connection, now: datetime | None = None) -> list[ImportStatus]:
    """Every manual source's state, due ones first, then the longest since import."""
    now = now or datetime.now(UTC)
    settings = {
        r[0]: (r[1], _parse(r[2]))
        for r in conn.execute("SELECT key, aim_days, snoozed_until FROM import_reminders")
    }
    out = []
    for source in MANUAL_SOURCES:
        aim, snoozed = settings.get(source.key, (None, None))
        aim = source.aim_days if aim is None else aim
        if snoozed is not None and snoozed <= now:
            snoozed = None
        last = last_import(conn, source)
        days = None if last is None else max(0, (now - last) // timedelta(days=1))
        readable = source.how is not None
        due = readable and aim > 0 and snoozed is None and (days is None or days >= aim)
        out.append(ImportStatus(source, last, days, aim, snoozed, due))
    # Due first; then longest since import, never-imported last
    out.sort(key=lambda s: (not s.due, s.days is None, -(s.days or 0)))
    return out


def set_aim(conn: sqlite3.Connection, key: str, aim_days: int | None) -> None:
    """Keep your aim for ``key``; None goes back to the default. Doesn't commit."""
    if key not in BY_KEY:
        raise KeyError(key)
    conn.execute(
        "INSERT INTO import_reminders (key, aim_days) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET aim_days = excluded.aim_days",
        (key, aim_days),
    )


def snooze(
    conn: sqlite3.Connection,
    key: str,
    days: int = DEFAULT_SNOOZE_DAYS,
    now: datetime | None = None,
) -> datetime:
    """Hold ``key``'s reminder for ``days``; 0 ends a snooze. Doesn't commit."""
    if key not in BY_KEY:
        raise KeyError(key)
    until = (now or datetime.now(UTC)) + timedelta(days=days)
    conn.execute(
        "INSERT INTO import_reminders (key, snoozed_until) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET snoozed_until = excluded.snoozed_until",
        (key, None if days == 0 else _utc_text(until)),
    )
    return until


@dataclass(frozen=True)
class FailingSync:
    source: str
    failed_runs: int  # in a row, newest back
    last_error: str | None
    last_ok: datetime | None  # the last sync that completed, if any


def _local(text: str | None) -> datetime | None:
    """A ``sync_log`` time, written in local time with no zone, as UTC."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (when.astimezone() if when.tzinfo is None else when).astimezone(UTC)


def failing_syncs(conn: sqlite3.Connection) -> list[FailingSync]:
    """Enabled sources whose last ``FAILED_RUNS`` syncs all failed."""
    out = []
    names = [r[0] for r in conn.execute("SELECT source_type FROM sources WHERE enabled = 1")]
    for name in sorted(names):
        runs = conn.execute(
            "SELECT status, error_message FROM sync_log WHERE source_type = ?"
            " AND status != 'running' ORDER BY id DESC LIMIT ?",
            (name, FAILED_RUNS),
        ).fetchall()
        if len(runs) < FAILED_RUNS or any(r[0] != "failed" for r in runs):
            continue
        streak = conn.execute(
            "SELECT count(*) FROM sync_log WHERE source_type = ? AND status = 'failed'"
            " AND id > coalesce((SELECT max(id) FROM sync_log WHERE source_type = ?"
            " AND status = 'completed'), 0)",
            (name, name),
        ).fetchone()[0]
        ok = conn.execute(
            "SELECT max(completed_at) FROM sync_log WHERE source_type = ? AND status = 'completed'",
            (name,),
        ).fetchone()[0]
        out.append(FailingSync(name, streak, runs[0][1], _local(ok)))
    return out

"""Import reminders: last import, aim, snooze and failing syncs (#403)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from lestash.core import import_reminders as r
from lestash.core.database import get_connection
from lestash.core.sync import SyncRun

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _by_key(conn, now=NOW) -> dict[str, r.ImportStatus]:
    return {s.source.key: s for s in r.statuses(conn, now)}


def _item(conn, source_type: str, fetched: datetime, n: int = 1) -> None:
    conn.execute(
        "INSERT INTO items (source_type, source_id, content, fetched_at) VALUES (?, ?, 'x', ?)",
        (source_type, f"{source_type}-{n}", fetched.strftime("%Y-%m-%d %H:%M:%S")),
    )


def test_nothing_imported_yet_is_due(test_db) -> None:
    with get_connection(test_db) as conn:
        state = _by_key(conn)
    assert set(state) == {s.key for s in r.MANUAL_SOURCES}
    assert all(s.last_import is None and s.days is None for s in state.values())
    due = {k for k, s in state.items() if s.due}
    assert due == {"google-takeout", "mistral", "linkedin-export"}


def test_an_import_lestash_cant_read_is_never_due(test_db) -> None:
    # The Claude export has no importer yet (#88): a reminder would ask the impossible
    with get_connection(test_db) as conn:
        _item(conn, "claude", NOW - timedelta(days=237))
        claude = _by_key(conn)["claude"]
    assert (claude.days, claude.aim_days, claude.due) == (237, 30, False)


def test_an_import_is_logged_as_an_activity_even_adding_nothing(test_db) -> None:
    with get_connection(test_db) as conn:
        assert r.record_import(conn, "mistral", 0, at=NOW - timedelta(days=2), name="m.zip")
        row = conn.execute(
            "SELECT item_id, occurred_at, source, details FROM activities WHERE verb = 'imported'"
        ).fetchone()
        # Like every activity: UTC to the millisecond, with its zone
        assert tuple(row)[:3] == (None, "2026-11-20T12:00:00.000Z", "import:mistral")
        assert '"name": "m.zip"' in row[3]
        mistral = _by_key(conn)["mistral"]
    assert (mistral.days, mistral.due) == (2, False)


def test_an_import_that_is_no_manual_source_is_not_logged(test_db) -> None:
    with get_connection(test_db) as conn:
        assert not r.record_import(conn, "json", 5)
        assert not r.record_import(conn, "claude-code", 5)
        assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 0


def test_every_takeout_product_counts_for_google_takeout(test_db) -> None:
    with get_connection(test_db) as conn:
        for kind in ("takeout", "gemini", "notebooklm", "google-keep", "youtube"):
            r.record_import(conn, kind, 1)
        sources = {row[0] for row in conn.execute("SELECT source FROM activities")}
    assert sources == {"import:google-takeout"}


def test_items_fetched_before_logging_began_count_as_an_import(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "claude", NOW - timedelta(days=237))
        _item(conn, "claude-project", NOW - timedelta(days=240), n=2)
        claude = _by_key(conn)["claude"]
    assert claude.last_import == NOW - timedelta(days=237)
    assert claude.days == 237


def test_a_gemini_chat_saved_from_a_page_is_no_takeout(test_db) -> None:
    # Gemini chats also come from a shared or uploaded page, so they don't count
    with get_connection(test_db) as conn:
        _item(conn, "gemini", NOW - timedelta(days=1))
        assert _by_key(conn)["google-takeout"].last_import is None


def _checkpoint(conn, key: str, status: str, requested: datetime) -> None:
    name = f"takeout-{requested:%Y%m%dT%H%M%S}Z-001.zip"
    conn.execute(
        "INSERT INTO sync_checkpoints (source, key, value, updated_at) VALUES ('takeout', ?, ?, ?)",
        (key, json.dumps({"name": name, "status": status}), NOW.strftime("%Y-%m-%d %H:%M:%S")),
    )


def test_only_imported_takeout_zips_count_by_when_they_were_asked_for(test_db) -> None:
    with get_connection(test_db) as conn:
        _checkpoint(conn, "a", "imported", NOW - timedelta(days=20))
        _checkpoint(conn, "b", "failed", NOW - timedelta(days=2))
        _checkpoint(conn, "c", "unrecognized", NOW - timedelta(days=3))
        conn.execute(  # a NotebookLM export index, rewritten today
            "INSERT INTO sync_checkpoints (source, key, value) VALUES"
            " ('takeout', 'notebooklm:takeout-x', '{\"indexed\": true}')"
        )
        # updated_at is now for all of them; the request time in the name decides
        assert _by_key(conn)["google-takeout"].days == 20


def test_a_zip_name_with_no_real_date_is_skipped() -> None:
    assert r._requested("takeout-20261399T250000Z-001.zip") is None
    assert r._requested("export.zip") is None
    assert r._requested("takeout-20261113T050346Z-1-001.zip") == datetime(
        2026, 11, 13, 5, 3, 46, tzinfo=UTC
    )


def test_the_latest_sign_wins(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "notebooklm", NOW - timedelta(days=90))
        _checkpoint(conn, "zip-1", "imported", NOW - timedelta(days=9))
        assert _by_key(conn)["google-takeout"].days == 9
        r.record_import(conn, "takeout", 0, at=NOW - timedelta(days=1))
        assert _by_key(conn)["google-takeout"].days == 1


def test_a_snooze_holds_the_reminder_until_it_ends(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "mistral", NOW - timedelta(days=100))
        r.snooze(conn, "mistral", 7, now=NOW)
        assert not _by_key(conn)["mistral"].due
        assert _by_key(conn, NOW + timedelta(days=8))["mistral"].due
        r.snooze(conn, "mistral", 0, now=NOW)
        assert _by_key(conn)["mistral"].snoozed_until is None


def test_your_aim_replaces_the_default_and_0_never_reminds(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "mistral", NOW - timedelta(days=40))
        assert _by_key(conn)["mistral"].due  # default 30
        r.set_aim(conn, "mistral", 60)
        assert (_by_key(conn)["mistral"].aim_days, _by_key(conn)["mistral"].due) == (60, False)
        r.set_aim(conn, "mistral", 0)
        assert not _by_key(conn)["mistral"].due
        r.set_aim(conn, "mistral", None)
        assert _by_key(conn)["mistral"].aim_days == 30


def test_due_ones_come_first_then_the_longest_since(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "mistral", NOW - timedelta(days=5))
        _item(conn, "claude", NOW - timedelta(days=200))
        _item(conn, "google-keep", NOW - timedelta(days=70))
        order = [s.source.key for s in r.statuses(conn, NOW)]
    # due: google-takeout 70, linkedin-export never; then claude 200 (unreadable), mistral 5
    assert order == ["google-takeout", "linkedin-export", "claude", "mistral"]


def test_unknown_keys_are_refused(test_db) -> None:
    import pytest

    with get_connection(test_db) as conn:
        with pytest.raises(KeyError):
            r.snooze(conn, "nope")
        with pytest.raises(KeyError):
            r.set_aim(conn, "nope", 3)


def _runs(conn, source: str, *statuses: str, enabled: int = 1) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sources (source_type, enabled) VALUES (?, ?)", (source, enabled)
    )
    for i, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO sync_log (source_type, started_at, completed_at, status, error_message)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                source,
                f"2026-06-{i + 1:02d} 10:00:00",
                f"2026-06-{i + 1:02d} 10:01:00",
                status,
                "boom" if status == "failed" else None,
            ),
        )


def test_a_source_whose_last_syncs_all_failed_is_failing(test_db) -> None:
    with get_connection(test_db) as conn:
        _runs(conn, "voice", "completed", "failed", "failed", "failed", "failed")
        _runs(conn, "kobo", "failed", "failed", "completed")
        _runs(conn, "claude-code", "failed", "failed", "failed", enabled=0)
        _runs(conn, "gmail", "failed", "failed")
        failing = r.failing_syncs(conn)
    assert [(f.source, f.failed_runs, f.last_error) for f in failing] == [("voice", 4, "boom")]
    assert failing[0].last_ok is not None


def test_a_takeout_sync_that_adds_items_counts_as_an_import(test_db) -> None:
    with get_connection(test_db) as conn:
        with SyncRun.start(conn, "takeout") as run:
            run.finish("completed", items_added=12)
        with SyncRun.start(conn, "takeout") as run:
            run.finish("completed", items_added=0)  # no new zip: nothing imported
        with SyncRun.start(conn, "youtube") as run:
            run.finish("completed", items_added=3)  # synced by itself: not an import
        rows = conn.execute("SELECT source, details FROM activities").fetchall()
    assert [row[0] for row in rows] == ["import:google-takeout"]
    assert '"items_added": 12' in rows[0][1]

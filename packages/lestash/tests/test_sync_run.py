"""A sync's sync_log row and its per-source lock (lestash.core.sync.SyncRun)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator

import pytest
import typer
from lestash.cli import sources as sources_cli
from lestash.core.database import get_connection, get_db_path
from lestash.core.sync import INTERRUPTED, SyncAlreadyRunning, SyncRun
from lestash.models.item import ItemCreate


def _rows(test_db) -> list[tuple[str, str, str | None, int]]:
    with get_connection(test_db) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT source_type, status, error_message, items_added FROM sync_log ORDER BY id"
            )
        ]


def _sync(test_db, monkeypatch, plugin) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": plugin})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    sources_cli.sync_source(source_name="audible", all_sources=False)


def test_a_dead_runs_row_is_marked_interrupted_by_the_next_run(test_db) -> None:
    with get_connection(test_db) as conn:
        for source in ("audible", "kobo"):
            conn.execute(
                "INSERT INTO sync_log (source_type, started_at, status) "
                "VALUES (?, '2026-01-01 10:00:00', 'running')",
                (source,),
            )
        conn.commit()
        with SyncRun.start(conn, "audible") as run:
            run.finish("completed", items_added=3)

    assert _rows(test_db) == [
        ("audible", "failed", INTERRUPTED, 0),
        ("kobo", "running", None, 0),  # another source's run is left alone
        ("audible", "completed", None, 3),
    ]


def test_a_second_run_of_the_same_source_is_refused(test_db) -> None:
    with get_connection(test_db) as conn, get_connection(test_db) as other:
        with SyncRun.start(conn, "audible") as run:
            with pytest.raises(SyncAlreadyRunning):
                SyncRun.start(other, "audible")
            with SyncRun.start(other, "kobo") as kobo:
                kobo.finish("completed")
            run.finish("completed")
        # Released: the next run starts
        with SyncRun.start(other, "audible") as again:
            again.finish("completed")

    assert [(source, status) for source, status, _, _ in _rows(test_db)] == [
        ("audible", "completed"),
        ("kobo", "completed"),
        ("audible", "completed"),
    ]


_HOLD_LOCK = """
import sys, time
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection
from lestash.core.sync import SyncRun
config = Config(general=GeneralConfig(database_path=sys.argv[1]))
with get_connection(config) as conn:
    run = SyncRun.start(conn, "audible")  # held: dropping it closes the lock
    print("started", flush=True)
    time.sleep(60)
"""


def test_a_killed_process_releases_its_lock(test_db) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(get_db_path(test_db))],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert "started" in iter(lambda: proc.stdout.readline().strip(), "")  # waits for it
        with get_connection(test_db) as conn:
            with pytest.raises(SyncAlreadyRunning):
                SyncRun.start(conn, "audible")
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)
            with SyncRun.start(conn, "audible") as run:
                run.finish("completed")
    finally:
        proc.kill()
        proc.wait()

    assert _rows(test_db) == [
        ("audible", "failed", INTERRUPTED, 0),
        ("audible", "completed", None, 0),
    ]


def _item(n: int) -> ItemCreate:
    return ItemCreate(source_type="audible", source_id=f"b{n}", content="x")


class _StopsAfterOne:
    def __init__(self, stop) -> None:
        self.stop = stop

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield _item(1)
        self.stop()
        yield _item(2)


def _ctrl_c() -> None:
    raise KeyboardInterrupt


def _sigterm() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


@pytest.mark.parametrize(("stop", "raised"), [(_ctrl_c, KeyboardInterrupt), (_sigterm, SystemExit)])
def test_a_stopped_cli_sync_is_logged_interrupted(test_db, monkeypatch, stop, raised) -> None:
    with pytest.raises(raised):
        _sync(test_db, monkeypatch, _StopsAfterOne(stop))

    # The item stored after the last commit is rolled back, and not counted
    assert _rows(test_db) == [("audible", "failed", INTERRUPTED, 0)]
    with get_connection(test_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # the handler is put back


class _Fails:
    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield _item(1)
        raise RuntimeError("API went away")


def test_a_failed_cli_sync_keeps_its_error(test_db, monkeypatch) -> None:
    with pytest.raises(typer.Exit) as exc:  # so systemd shows the failure (#209)
        _sync(test_db, monkeypatch, _Fails())
    assert exc.value.exit_code == 1
    assert _rows(test_db) == [("audible", "failed", "API went away", 1)]


class _Works:
    def sync(self, config: dict) -> Iterator[ItemCreate]:
        yield ItemCreate(source_type="kobo", source_id="k1", content="x")


def test_an_all_sources_cli_sync_runs_every_source_then_exits_1_if_one_failed(
    test_db, monkeypatch
) -> None:
    plugins = {"audible": _Fails(), "kobo": _Works()}
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: plugins)
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    with pytest.raises(typer.Exit) as exc:
        sources_cli.sync_source(source_name=None, all_sources=True)
    assert exc.value.exit_code == 1
    assert [(s, status) for s, status, _, _ in _rows(test_db)] == [
        ("audible", "failed"),
        ("kobo", "completed"),  # still ran after the failure
    ]


def test_an_error_starting_one_source_does_not_stop_the_others(test_db, monkeypatch) -> None:
    import sqlite3

    real_start = SyncRun.start

    def start(conn, source, *args, **kwargs):
        if source == "audible":
            raise sqlite3.OperationalError("database is locked")
        return real_start(conn, source, *args, **kwargs)

    monkeypatch.setattr(SyncRun, "start", staticmethod(start))
    monkeypatch.setattr(
        sources_cli, "load_plugins", lambda: {"audible": _Works(), "kobo": _Works()}
    )
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    with pytest.raises(typer.Exit) as exc:
        sources_cli.sync_source(source_name=None, all_sources=True)
    assert exc.value.exit_code == 1
    assert [(s, status) for s, status, _, _ in _rows(test_db)] == [("kobo", "completed")]


def test_an_unknown_source_exits_1(test_db, monkeypatch) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"kobo": _Works()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    with pytest.raises(typer.Exit) as exc:
        sources_cli.sync_source(source_name="kbo", all_sources=False)
    assert exc.value.exit_code == 1


def test_a_named_cli_sync_waits_for_a_running_one(test_db, monkeypatch) -> None:
    """The Kobo plug-in sync relies on this: a second sync waits, then runs."""
    import threading

    started, release = threading.Event(), threading.Event()

    class _Slow:
        def sync(self, config: dict) -> Iterator[ItemCreate]:
            started.set()
            release.wait(10)
            yield _item(1)

    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": _Slow()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    first = threading.Thread(target=sources_cli.sync_source, args=("audible", False))
    first.start()
    assert started.wait(10)
    waiting = []
    monkeypatch.setattr(sources_cli, "_say_waiting", lambda name: lambda: waiting.append(name))
    second = threading.Thread(target=sources_cli.sync_source, args=("audible", False))
    second.start()
    second.join(1)
    assert second.is_alive() and waiting == ["audible"]  # blocked on the lock
    release.set()
    first.join(10)
    second.join(10)

    assert [status for _, status, _, _ in _rows(test_db)] == ["completed", "completed"]


def test_an_all_sources_cli_sync_skips_a_running_one(test_db, monkeypatch) -> None:
    monkeypatch.setattr(sources_cli, "load_plugins", lambda: {"audible": _Fails()})
    monkeypatch.setattr(sources_cli.Config, "load", classmethod(lambda cls: test_db))
    with get_connection(test_db) as conn, SyncRun.start(conn, "audible") as run:
        sources_cli.sync_source(source_name=None, all_sources=True)
        run.finish("completed")
    assert _rows(test_db) == [("audible", "completed", None, 0)]


def test_a_run_stopped_after_its_items_are_stored_counts_them(test_db) -> None:
    from lestash.core.sync import SyncStore

    with get_connection(test_db) as conn:
        store = SyncStore(conn, "audible")
        with pytest.raises(KeyboardInterrupt), SyncRun.start(conn, "audible", store):
            store.run(iter([_item(1), _item(2), _item(3)]))
            raise KeyboardInterrupt  # e.g. during the post-sync steps

    assert _rows(test_db) == [("audible", "failed", INTERRUPTED, 3)]

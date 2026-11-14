"""POST /api/embeddings/rebuild logs its run with the syncs, and says so when a
rebuild from the CLI already holds the lock (#309)."""

import time

from lestash.core.database import get_connection
from lestash.core.embeddings import LOG_SOURCE
from lestash.core.sync import SyncRun
from lestash_server.routes import embeddings as route


def _log(config):
    with get_connection(config) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT status, items_added FROM sync_log WHERE source_type = ? ORDER BY id",
                (LOG_SOURCE,),
            )
        ]


def _wait_until_done():
    for _ in range(100):
        if not route._rebuild_running:
            return
        time.sleep(0.05)


def test_a_web_rebuild_is_logged_as_completed(client, test_config, monkeypatch):
    monkeypatch.setattr(route, "rebuild_embeddings", lambda conn: 3)

    response = client.post("/api/embeddings/rebuild")
    _wait_until_done()

    assert response.json()["status"] == "started"
    assert _log(test_config) == [("completed", 3)]


def test_a_web_rebuild_while_the_cli_rebuilds_says_already_running(
    client, test_config, monkeypatch
):
    def never(conn):
        raise AssertionError("rebuilt while the lock was held")

    monkeypatch.setattr(route, "rebuild_embeddings", never)

    with get_connection(test_config) as conn, SyncRun.start(conn, LOG_SOURCE) as held:
        response = client.post("/api/embeddings/rebuild")
        _wait_until_done()
        held.finish("completed", items_added=0, items_updated=0)

    assert response.json()["status"] == "already_running"
    assert _log(test_config) == [("completed", 0)]  # only the CLI's run


def test_a_second_web_rebuild_during_one_answers_at_once(client, test_config, monkeypatch):
    import threading

    release = threading.Event()
    calls = []

    def slow(conn):
        calls.append(1)
        release.wait(timeout=10)
        return 1

    monkeypatch.setattr(route, "rebuild_embeddings", slow)

    first = client.post("/api/embeddings/rebuild")
    started = time.monotonic()
    second = client.post("/api/embeddings/rebuild")
    waited = time.monotonic() - started
    release.set()
    _wait_until_done()

    assert first.json()["status"] == "started"
    assert second.json()["status"] == "already_running"
    assert waited < 5
    assert calls == [1]
    assert _log(test_config) == [("completed", 1)]


def test_a_web_rebuild_that_fails_part_way_logs_the_count_kept(client, test_config, monkeypatch):
    from lestash.core.embeddings import RefreshCounts, RefreshStopped

    def stops(conn):
        raise RefreshStopped("stopped after 2 of 5: disk full", RefreshCounts(embedded=2))

    monkeypatch.setattr(route, "rebuild_embeddings", stops)

    client.post("/api/embeddings/rebuild")
    _wait_until_done()

    with get_connection(test_config) as conn:
        row = conn.execute(
            "SELECT status, items_added, error_message FROM sync_log WHERE source_type = ?",
            (LOG_SOURCE,),
        ).fetchone()
    assert tuple(row) == ("failed", 2, "stopped after 2 of 5: disk full")


def test_a_thread_that_cannot_start_gives_the_lock_back(client, monkeypatch):
    import pytest

    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    import threading
    from types import SimpleNamespace

    # Only the route's view of threading: the test client needs real threads
    monkeypatch.setattr(route, "threading", SimpleNamespace(Thread=NoThread, Event=threading.Event))
    with pytest.raises(RuntimeError):
        client.post("/api/embeddings/rebuild")

    assert not route._rebuild_running
    assert route._rebuild_lock.acquire(blocking=False)
    route._rebuild_lock.release()

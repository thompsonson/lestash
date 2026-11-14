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

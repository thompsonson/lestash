"""Two connections that start together run each pending migration once (#371)."""

import sqlite3
import threading
import time

import lestash.core.youtube_videos as youtube_videos
from lestash.core.database import (
    SCHEMA_VERSION,
    get_connection,
    get_db_path,
    get_schema_version,
)


def test_two_connections_run_a_pending_migration_once(test_db, monkeypatch):
    db_path = get_db_path(test_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 35")

    runs = []

    def slow_fold(conn):
        runs.append(1)
        time.sleep(0.5)  # long enough for the other connection to read the version
        return 0

    monkeypatch.setattr(youtube_videos, "fold_video_copies", slow_fold)
    errors = []

    def open_connection():
        try:
            with get_connection(test_db):
                pass
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=open_connection) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(runs) == 1
    with sqlite3.connect(db_path) as conn:
        assert get_schema_version(conn) == SCHEMA_VERSION


def test_an_in_memory_database_migrates_without_a_lock():
    from lestash.core.database import SCHEMA, apply_migrations

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    apply_migrations(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

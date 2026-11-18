"""Two connections that start together run each pending migration once (#371)."""

import logging
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


def test_database_logging_during_a_migration_does_not_hang(test_db):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 35")

    logger = logging.getLogger("lestash.core.database")
    handler = DatabaseHandler(test_db)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    done = threading.Event()

    def open_connection():
        with get_connection(test_db):
            done.set()

    try:
        t = threading.Thread(target=open_connection, daemon=True)
        t.start()
        assert done.wait(20), "migration hung on its own lock"
        handler.flush()  # as logging does at exit
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    with sqlite3.connect(db_path) as conn:
        assert get_schema_version(conn) == SCHEMA_VERSION
        messages = [m for (m,) in conn.execute("SELECT message FROM log_entries")]
    # The migration's own lines are kept, written once it was done
    assert any(m.startswith("Applying migration 36") for m in messages)
    assert "Migration 36 complete" in messages


def test_a_thread_logging_while_another_migrates_does_not_deadlock(test_db, monkeypatch):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 35")
    logger = logging.getLogger("lestash.test.deadlock")
    handler = DatabaseHandler(test_db)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    started = threading.Event()

    def slow_fold(conn):
        started.set()
        for _ in range(5):
            logger.info("migrating")  # holds the write lock, then the handler's lock
            time.sleep(0.1)
        return 0

    monkeypatch.setattr(youtube_videos, "fold_video_copies", slow_fold)

    def migrate():
        with get_connection(test_db):
            pass

    def log_meanwhile():
        started.wait(10)
        for _ in range(5):
            logger.info("meanwhile")
            time.sleep(0.05)

    threads = [threading.Thread(target=f, daemon=True) for f in (migrate, log_meanwhile)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert not any(t.is_alive() for t in threads), "deadlocked"
        handler.flush()
    finally:
        logger.removeHandler(handler)
    with sqlite3.connect(db_path) as conn:
        messages = [m for (m,) in conn.execute("SELECT message FROM log_entries")]
    assert messages.count("migrating") == 5
    assert messages.count("meanwhile") == 5


def test_records_the_database_is_too_busy_for_are_written_with_the_next(test_db):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    handler = DatabaseHandler(test_db)

    def record(msg):
        return logging.LogRecord("lestash.test", logging.INFO, __file__, 1, msg, None, None)

    busy = sqlite3.connect(db_path)
    busy.execute("BEGIN IMMEDIATE")
    handler.emit(record("first"))
    busy.rollback()
    busy.close()
    handler.emit(record("second"))
    with sqlite3.connect(db_path) as conn:
        messages = [m for (m,) in conn.execute("SELECT message FROM log_entries ORDER BY id")]
    assert messages == ["first", "second"]


def test_logging_before_the_database_exists_does_not_create_it(tmp_path):
    from lestash.core.config import Config, GeneralConfig
    from lestash.core.logging import DatabaseHandler

    config = Config(general=GeneralConfig(database_path=str(tmp_path / "new.db")))
    handler = DatabaseHandler(config)
    handler.emit(logging.LogRecord("lestash.test", logging.INFO, __file__, 1, "x", None, None))
    assert not (tmp_path / "new.db").exists()


def test_a_record_that_cannot_be_written_does_not_block_the_rest(test_db):
    from lestash.core.logging import DatabaseHandler

    handler = DatabaseHandler(test_db)

    def record(msg):
        return logging.LogRecord("lestash.test", logging.INFO, __file__, 1, msg, None, None)

    handler.emit(record("bad \udc80"))  # a lone surrogate: SQLite cannot store it
    handler.emit(record("after"))
    with sqlite3.connect(get_db_path(test_db)) as conn:
        messages = [m for (m,) in conn.execute("SELECT message FROM log_entries")]
    assert messages == ["after"]
    assert handler._held == []


def test_a_record_waits_briefly_for_a_busy_database(test_db):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    handler = DatabaseHandler(test_db)
    busy = sqlite3.connect(db_path, check_same_thread=False)
    busy.execute("BEGIN IMMEDIATE")
    threading.Timer(0.3, busy.rollback).start()
    handler.emit(logging.LogRecord("lestash.test", logging.INFO, __file__, 1, "x", None, None))
    busy.close()
    with sqlite3.connect(db_path) as conn:
        assert [m for (m,) in conn.execute("SELECT message FROM log_entries")] == ["x"]


def test_records_held_while_the_database_is_busy_are_retried_on_their_own(test_db):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    handler = DatabaseHandler(test_db)
    handler.WAIT_S = 0
    handler.RETRY_S = 0.2
    busy = sqlite3.connect(db_path)
    busy.execute("BEGIN IMMEDIATE")
    handler.emit(logging.LogRecord("lestash.test", logging.INFO, __file__, 1, "x", None, None))
    time.sleep(0.7)  # the first retries find it still busy, and schedule more
    busy.rollback()
    busy.close()
    deadline = time.monotonic() + 10
    while handler._held and time.monotonic() < deadline:
        time.sleep(0.1)
    with sqlite3.connect(db_path) as conn:
        assert [m for (m,) in conn.execute("SELECT message FROM log_entries")] == ["x"]


def test_a_closed_handler_stops_retrying(test_db):
    from lestash.core.logging import DatabaseHandler

    db_path = get_db_path(test_db)
    handler = DatabaseHandler(test_db)
    handler.WAIT_S = 0
    handler.RETRY_S = 0.1
    busy = sqlite3.connect(db_path)
    busy.execute("BEGIN IMMEDIATE")
    try:
        handler.emit(logging.LogRecord("lestash.test", logging.INFO, __file__, 1, "x", None, None))
        handler.close()  # still busy: the last try fails
        time.sleep(0.3)
        assert handler._retry is None
    finally:
        busy.rollback()
        busy.close()

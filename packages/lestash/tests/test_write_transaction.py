"""Short writes: begin_write and write_transaction (#401)."""

from __future__ import annotations

import sqlite3
import threading

import lestash.core.database as database
import pytest
from lestash.core.database import begin_write, get_connection, get_db_path, write_transaction


def _sources(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT source_type FROM sources ORDER BY 1")]


def _add_source(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT INTO sources (source_type) VALUES (?)", (name,))


def test_a_write_is_committed(test_db) -> None:
    with get_connection(test_db) as conn:
        with write_transaction(conn):
            _add_source(conn, "a")
        assert not conn.in_transaction
    with get_connection(test_db) as other:
        assert _sources(other) == ["a"]


def test_a_failed_write_is_rolled_back(test_db) -> None:
    with get_connection(test_db) as conn:
        with pytest.raises(ValueError), write_transaction(conn):
            _add_source(conn, "a")
            raise ValueError("boom")
        assert _sources(conn) == []


def test_a_write_already_open_is_refused(test_db) -> None:
    """The lock may have been held since that earlier write, so say so."""
    with get_connection(test_db) as conn:
        _add_source(conn, "a")  # opens a transaction, not committed
        with pytest.raises(RuntimeError, match="already open"):
            begin_write(conn)


def _holder(path, release: threading.Event, held: threading.Event) -> None:
    conn = sqlite3.connect(path)
    conn.execute("BEGIN IMMEDIATE")
    held.set()
    release.wait(5)
    conn.rollback()
    conn.close()


def test_a_locked_write_is_tried_once_more(test_db, monkeypatch) -> None:
    path = get_db_path(test_db)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_holder, args=(path, release, held))
    holder.start()
    held.wait(5)
    # The other writer lets go during the pause between the two tries
    monkeypatch.setattr(database, "LOCK_RETRY_PAUSE_S", 0.5)
    threading.Timer(0.3, release.set).start()
    conn = sqlite3.connect(path, timeout=0.1)
    try:
        with write_transaction(conn):
            _add_source(conn, "a")
        assert _sources(conn) == ["a"]
    finally:
        conn.close()
        holder.join()


def test_a_write_still_locked_on_the_second_try_fails(test_db, monkeypatch) -> None:
    path = get_db_path(test_db)
    release, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_holder, args=(path, release, held))
    holder.start()
    held.wait(5)
    monkeypatch.setattr(database, "LOCK_RETRY_PAUSE_S", 0.0)
    conn = sqlite3.connect(path, timeout=0.1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            begin_write(conn)
    finally:
        release.set()
        conn.close()
        holder.join()

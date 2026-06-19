"""Tests for enabling/disabling sources so `sync --all` can skip them."""

from __future__ import annotations

from lestash.cli.sources import _disabled_sources, _set_source_enabled
from lestash.core.database import get_connection


def test_disable_then_enable_roundtrip(test_db) -> None:
    config = test_db

    # Nothing disabled to start.
    with get_connection(config) as conn:
        assert _disabled_sources(conn) == set()

    # Disabling creates the row (source need not exist yet).
    _set_source_enabled("claude-code", False, config)
    with get_connection(config) as conn:
        assert "claude-code" in _disabled_sources(conn)

    # Re-enabling clears it.
    _set_source_enabled("claude-code", True, config)
    with get_connection(config) as conn:
        assert _disabled_sources(conn) == set()


def test_disable_is_idempotent_and_isolated(test_db) -> None:
    config = test_db
    _set_source_enabled("claude-code", False, config)
    _set_source_enabled("claude-code", False, config)  # again — no duplicate row
    _set_source_enabled("youtube", False, config)

    with get_connection(config) as conn:
        assert _disabled_sources(conn) == {"claude-code", "youtube"}
        rows = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE source_type = 'claude-code'"
        ).fetchone()[0]
        assert rows == 1

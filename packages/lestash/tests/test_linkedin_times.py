"""Migration 26 (#236): LinkedIn times in UTC, and others' actions not yours."""

from __future__ import annotations

import json
from datetime import datetime

from lestash.core.database import SCHEMA_VERSION, apply_migrations, get_connection
from lestash.core.links import aware_time

ME, ANN = "urn:li:person:me", "urn:li:person:ann"


def _row(conn, n: int, created_at: str, actor: str | None = ME, owner: str | None = ME) -> int:
    raw = {k: v for k, v in (("actor", actor), ("owner", owner)) if v}
    return conn.execute(
        "INSERT INTO items (source_type, source_id, content, created_at, is_own_content, "
        "metadata) VALUES ('linkedin', ?, 'x', ?, 1, ?) RETURNING id",
        (f"changelog-{n}", created_at, json.dumps({"source": "changelog", "raw": raw})),
    ).fetchone()[0]


def test_migration_26_makes_linkedin_times_utc_and_others_not_yours(test_db) -> None:
    with get_connection(test_db) as conn:
        winter = _row(conn, 1, "2026-01-19 10:49:34.619000")  # CET, UTC+1
        summer = _row(conn, 2, "2026-07-22 07:20:50")  # CEST, UTC+2
        utc = _row(conn, 3, "2025-04-06 12:15:25+00:00")  # the export's, already UTC
        theirs = _row(conn, 4, "2026-11-11 18:18:16", actor=ANN)
        no_actor = _row(conn, 5, "2026-03-01 10:00:00", actor=None)
        posted = conn.execute(
            "INSERT INTO items (source_type, source_id, content, created_at, metadata) "
            "VALUES ('linkedin', 'urn:li:share:1', 'x', '2026-04-03T14:22:10.245590', ?) "
            "RETURNING id",
            (json.dumps({"post_urn": "urn:li:share:1", "resource_name": "ugcPosts"}),),
        ).fetchone()[0]
        snapshot = conn.execute(
            "INSERT INTO items (source_type, source_id, content, created_at, metadata) "
            "VALUES ('linkedin', 's', 'x', '2024-01-01 10:00:00', ?) RETURNING id",
            (json.dumps({"domain": "MEMBER_SHARE_INFO"}),),
        ).fetchone()[0]
        other = conn.execute(
            "INSERT INTO items (source_type, content, created_at) "
            "VALUES ('youtube', 'v', '2026-01-19 10:00:00') RETURNING id"
        ).fetchone()[0]
        conn.execute("PRAGMA user_version = 25")
        conn.commit()

        assert apply_migrations(conn) == SCHEMA_VERSION - 25

        def get(item_id: int) -> tuple:
            return tuple(
                conn.execute(
                    "SELECT created_at, is_own_content FROM items WHERE id = ?", (item_id,)
                ).fetchone()
            )

        assert get(winter) == ("2026-01-19 09:49:34.619000+00:00", 1)
        assert get(summer) == ("2026-07-22 05:20:50+00:00", 1)
        assert get(utc) == ("2025-04-06 12:15:25+00:00", 1)
        assert get(theirs) == ("2026-11-11 17:18:16+00:00", 0)
        assert get(no_actor)[1] == 1
        # A post made from LeStash wrote local time too: CEST, UTC+2
        assert get(posted)[0] == "2026-04-03 12:22:10.245590+00:00"
        # The snapshot fetch wrote UTC; other sources' times are left as they are
        assert get(snapshot)[0] == "2024-01-01 10:00:00"
        assert get(other)[0] == "2026-01-19 10:00:00"
        # Run again: nothing more to change
        assert apply_migrations(conn) == 0


def test_a_time_without_a_zone_is_utc() -> None:
    assert aware_time("2026-01-19 10:00:00", "linkedin") == datetime.fromisoformat(
        "2026-01-19 10:00:00+00:00"
    )

"""LinkedIn member names: person_profiles moved into people and their accounts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lestash.core.agents import link_agents
from lestash.core.database import (
    SCHEMA_VERSION,
    apply_migrations,
    delete_person_profile,
    get_connection,
    get_person_profile,
    list_person_profiles,
    upsert_person_profile,
)
from lestash.core.enrichment import resolve_author

ME = "urn:li:person:me"
RAY = "urn:li:person:ray"


def _comment(conn: sqlite3.Connection, source_id: str, text: str, tags: list[tuple]) -> int:
    """A comment of yours whose text tags members: (start, length, URN)."""
    attributes = [
        {
            "start": start,
            "length": length,
            "value": {"com.linkedin.common.MemberAttributedEntity": {"member": urn}},
        }
        for start, length, urn in tags
    ]
    metadata: dict[str, Any] = {
        "raw": {"owner": ME, "activity": {"message": {"text": text, "attributes": attributes}}}
    }
    return conn.execute(
        "INSERT INTO items (source_type, source_id, content, author, metadata) "
        "VALUES ('linkedin', ?, ?, ?, ?) RETURNING id",
        (source_id, text, ME, json.dumps(metadata)),
    ).fetchone()[0]


def _name(conn: sqlite3.Connection, urn: str) -> tuple[str, str | None]:
    return tuple(
        conn.execute(
            "SELECT p.name, p.name_origin FROM people p JOIN person_identities i "
            "ON i.person_id = p.id WHERE i.platform = 'linkedin' AND i.identifier = ?",
            (urn,),
        ).fetchone()
    )


def _account(conn: sqlite3.Connection, urn: str, name: str) -> None:
    person = conn.execute(
        "INSERT INTO people (name, kind) VALUES (?, 'person') RETURNING id", (name,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO person_identities (person_id, platform, identifier, display_name, status, "
        "origin) VALUES (?, 'linkedin', ?, ?, 'confirmed', 'import:linkedin')",
        (person, urn, name),
    )


def test_migration_20_moves_profiles_onto_accounts_and_drops_the_table(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.executescript(
            """
            ALTER TABLE people DROP COLUMN name_origin;
            CREATE TABLE person_profiles (
                id INTEGER PRIMARY KEY, urn TEXT UNIQUE NOT NULL, profile_url TEXT,
                display_name TEXT, source TEXT DEFAULT 'manual',
                created_at DATETIME, updated_at DATETIME
            );
            """
        )
        _account(conn, RAY, "Ray")  # named by the pass
        _account(conn, "urn:li:person:ann", "urn:li:person:ann")  # named by URN
        _account(conn, "urn:li:person:bo", "Bo Smith")
        conn.executemany(
            "INSERT INTO person_profiles (urn, display_name, profile_url, source) "
            "VALUES (?, ?, ?, ?)",
            [
                (RAY, "Raymond Myers", "https://linkedin.com/in/ray", "web"),
                ("urn:li:person:ann", "Ann Lee", None, "mention"),
                ("urn:li:person:bo", "Bo S", None, "mention"),
                ("urn:li:person:new", "Cy Ng", "https://linkedin.com/in/cy", "manual"),
            ],
        )
        conn.execute("PRAGMA user_version = 19")
        conn.commit()
        assert apply_migrations(conn) == SCHEMA_VERSION - 19
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "person_profiles" not in tables
        # A name you set is the person's, and kept; a script's only names the unnamed
        assert _name(conn, RAY) == ("Raymond Myers", "web")
        assert _name(conn, "urn:li:person:ann") == ("Ann Lee", None)
        assert _name(conn, "urn:li:person:bo") == ("Bo Smith", None)
        assert _name(conn, "urn:li:person:new") == ("Cy Ng", "manual")
        assert get_person_profile(conn, RAY) == {
            "urn": RAY,
            "profile_url": "https://linkedin.com/in/ray",
            "display_name": "Raymond Myers",
            "source": "web",
        }
        assert get_person_profile(conn, "urn:li:person:new")["profile_url"] == (
            "https://linkedin.com/in/cy"
        )


def test_the_agents_pass_keeps_a_name_you_set(test_db) -> None:
    with get_connection(test_db) as conn:
        _comment(conn, "c1", "Ray thanks", [(0, 3, RAY)])
        link_agents(conn)
        assert _name(conn, RAY) == ("Ray", None)
        upsert_person_profile(conn, RAY, display_name="Raymond", source="web")
        _comment(conn, "c2", "Ray Myers yes", [(0, 9, RAY)])
        link_agents(conn)
        assert _name(conn, RAY) == ("Raymond", "web")
        assert resolve_author(conn, RAY) == "Raymond"
        # Once you stop keeping it, the member takes their LinkedIn name
        assert delete_person_profile(conn, RAY)
        assert not delete_person_profile(conn, RAY)
        assert _name(conn, RAY) == ("Ray Myers", None)
        _comment(conn, "c3", "Ray Myers Jr yes", [(0, 12, RAY)])
        link_agents(conn)
        assert _name(conn, RAY) == ("Ray Myers Jr", None)


def test_a_script_name_does_not_replace_a_known_one(test_db) -> None:
    with get_connection(test_db) as conn:
        _comment(conn, "c1", "Ray Myers yes", [(0, 9, RAY)])
        link_agents(conn)
        upsert_person_profile(conn, RAY, display_name="Ray M", source="mention")
        assert _name(conn, RAY) == ("Ray Myers", None)


def test_naming_a_member_not_yet_seen_makes_their_account(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_person_profile(conn, RAY, profile_url="https://linkedin.com/in/ray")
        assert _name(conn, RAY) == (RAY, None)
        assert resolve_author(conn, RAY) == RAY  # a URL alone gives no name
        upsert_person_profile(conn, RAY, display_name="Ray Myers")
        _comment(conn, "c1", "Ray thanks", [(0, 3, RAY)])
        assert link_agents(conn) == 2  # the comment's author (you) and the mention
        assert _name(conn, RAY) == ("Ray Myers", "manual")
        assert {p["urn"]: p["display_name"] for p in list_person_profiles(conn)} == {
            RAY: "Ray Myers",
            ME: "You",
        }


def test_your_own_urn_names_you(test_db) -> None:
    with get_connection(test_db) as conn:
        _comment(conn, "c1", "hello", [])
        upsert_person_profile(conn, ME, display_name="Matthew Thompson")
        link_agents(conn)
        assert _name(conn, ME) == ("Matthew Thompson", "manual")
        assert conn.execute("SELECT count(*) FROM people WHERE is_self = 1").fetchone()[0] == 1
        # Your name stays when you stop keeping it
        assert delete_person_profile(conn, ME)
        assert _name(conn, ME) == ("Matthew Thompson", None)


def test_forgetting_a_name_set_for_an_unseen_member_returns_to_the_urn(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_person_profile(conn, RAY, display_name="Ray Myers", source="web")
        assert delete_person_profile(conn, RAY)
        assert _name(conn, RAY) == (RAY, None)
        assert get_person_profile(conn, RAY) is None


def test_an_account_not_confirmed_is_named_by_no_one(test_db) -> None:
    with get_connection(test_db) as conn:
        _account(conn, RAY, "Alice")
        conn.execute("UPDATE person_identities SET status = 'suggested'")
        upsert_person_profile(conn, RAY, display_name="Bob", source="web")
        assert conn.execute("SELECT name FROM people").fetchone()[0] == "Alice"
        assert get_person_profile(conn, RAY) is None
        assert not delete_person_profile(conn, RAY)


def test_naming_your_urn_before_its_changelog_still_makes_it_yours(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_person_profile(conn, ME, display_name="Matthew Thompson")
        _comment(conn, "c1", "hello", [])
        link_agents(conn)
        assert (
            conn.execute(
                "SELECT p.is_self FROM people p JOIN person_identities i ON i.person_id = p.id "
                "WHERE i.identifier = ?",
                (ME,),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM people").fetchone()[0] == 1
        assert _name(conn, ME) == ("Matthew Thompson", "manual")

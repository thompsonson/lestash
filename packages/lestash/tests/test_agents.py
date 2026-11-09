"""Agents: the people, channels and software that made, voiced or received each item."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lestash.core.agents import agents_for, link_agents
from lestash.core.database import SCHEMA_VERSION, apply_migrations, get_connection

ME = "urn:li:person:me"


def _item(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    author: str | None = None,
    metadata: dict[str, Any] | None = None,
    own: bool = False,
) -> int:
    return conn.execute(
        "INSERT INTO items (source_type, source_id, content, author, metadata, is_own_content) "
        "VALUES (?, ?, 'x', ?, ?, ?) RETURNING id",
        (source_type, source_id, author, json.dumps(metadata) if metadata else None, own),
    ).fetchone()[0]


def _agents(conn: sqlite3.Connection, item_id: int) -> list[tuple]:
    return [
        (a["role"], a["position"], a["person"]["name"], a["person"]["kind"], a["person"]["is_self"])
        for a in agents_for(conn, item_id)
    ]


def test_migration_19_adds_the_people_tables_and_activity_actors(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.executescript(
            "DROP TABLE item_mentions; DROP TABLE item_agents; DROP TABLE person_identities; "
            "ALTER TABLE activities DROP COLUMN actor_id; DROP TABLE people;"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        assert apply_migrations(conn) == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"people", "person_identities", "item_agents", "item_mentions"} <= tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
        assert "actor_id" in columns


def test_a_channel_is_one_organization_for_all_its_videos(test_db) -> None:
    channel = {"channel_id": "UC1", "channel_title": "BBC News"}
    url_only = {"channel_url": "https://www.youtube.com/channel/UC1", "channel_title": "BBC"}
    with get_connection(test_db) as conn:
        one = _item(conn, "youtube", "history:a", "BBC News", channel)
        two = _item(conn, "youtube", "history:b", "BBC", url_only)
        name_only = _item(conn, "youtube", "history:c", "BBC News", {"channel_title": "BBC"})
        assert link_agents(conn) == 2
        assert _agents(conn, one) == [("creator", None, "BBC News", "organization", False)]
        assert _agents(conn, two) == _agents(conn, one)
        # A name alone could be any of several channels
        assert _agents(conn, name_only) == []
        [account] = agents_for(conn, one)[0]["accounts"]
        assert account == {
            "platform": "youtube",
            "identifier": "UC1",
            "url": "https://www.youtube.com/channel/UC1",
        }
        assert link_agents(conn) == 0


def test_your_accounts_are_one_person_named_from_your_profile(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO person_profiles (urn, display_name) VALUES (?, 'Matthew Thompson')",
            (ME,),
        )
        mine = _item(conn, "linkedin", "p1", ME, {"raw": {"owner": ME}})
        theirs = _item(conn, "linkedin", "m1", "urn:li:person:x", {"raw": {"owner": ME}})
        company = _item(conn, "linkedin", "l1", "urn:li:company:9", {"raw": {"owner": ME}})
        prompt = _item(conn, "gemini", "g1", "user")
        reply = _item(conn, "gemini", "g2", "model")
        claude = _item(conn, "claude", "c1", None, {"sender": "assistant"})
        post = _item(
            conn,
            "bluesky",
            "b1",
            "Matthew Thompson",
            {"author_did": "did:plc:me", "author_handle": "me.example"},
            own=True,
        )
        link_agents(conn)
        you = ("author", None, "Matthew Thompson", "person", True)
        assert _agents(conn, mine) == [you]
        assert _agents(conn, prompt) == [you]
        assert _agents(conn, post) == [you]
        assert _agents(conn, theirs) == [("author", None, "urn:li:person:x", "person", False)]
        assert _agents(conn, company) == [
            ("author", None, "urn:li:company:9", "organization", False)
        ]
        assert _agents(conn, reply) == [("author", None, "Gemini", "software", False)]
        assert _agents(conn, claude) == [("author", None, "Claude", "software", False)]
        assert conn.execute("SELECT COUNT(*) FROM people WHERE is_self = 1").fetchone()[0] == 1
        accounts = {a["platform"] for a in agents_for(conn, mine)[0]["accounts"]}
        assert accounts == {"linkedin", "gemini", "bluesky"}


def test_papers_and_books_keep_their_order_and_roles(test_db) -> None:
    with get_connection(test_db) as conn:
        paper = _item(conn, "arxiv", "2401.1", "A et al.", {"authors": ["Ann Lee", "Bo Chan"]})
        book = _item(
            conn,
            "audible",
            "B01",
            "Brian Christian, Tom Griffiths",
            {
                "authors": ["Brian Christian", "Tom Griffiths"],
                "narrators": ["Brian Christian"],
                "publisher": "Brilliance Audio",
            },
        )
        link_agents(conn)
        assert _agents(conn, paper) == [
            ("author", 1, "Ann Lee", "person", False),
            ("author", 2, "Bo Chan", "person", False),
        ]
        assert _agents(conn, book) == [
            ("author", 1, "Brian Christian", "person", False),
            ("author", 2, "Tom Griffiths", "person", False),
            ("narrator", 1, "Brian Christian", "person", False),
            ("publisher", None, "Brilliance Audio", "organization", False),
        ]
        # Known by name only: no account, and one person per item, as a name can be
        # several people; a second pass makes none again
        assert agents_for(conn, paper)[0]["accounts"] == []
        other = _item(conn, "arxiv", "2401.2", None, {"authors": ["Ann Lee"]})
        assert link_agents(conn) == 1
        ann = {agents_for(conn, i)[0]["person"]["id"] for i in (paper, other)}
        assert len(ann) == 2


def _profile(conn: sqlite3.Connection, name: str) -> None:
    """Your LinkedIn profile, which names you: until your name is known, no email is yours."""
    conn.execute("INSERT INTO person_profiles (urn, display_name) VALUES (?, ?)", (ME, name))
    _item(conn, "linkedin", f"profile:{name}", ME, {"raw": {"owner": ME}})


def test_an_email_has_its_sender_and_recipient(test_db) -> None:
    with get_connection(test_db) as conn:
        _profile(conn, "Matthew Thompson")
        sent = _item(
            conn,
            "email",
            "e1",
            "Matthew Thompson",
            {"from_email": "Me@Example.org", "to_email": "ann@example.org", "to_name": "Ann"},
            own=True,
        )
        link_agents(conn)
        assert _agents(conn, sent) == [
            ("author", None, "Matthew Thompson", "person", True),
            ("recipient", None, "Ann", "person", False),
        ]


def test_a_deleted_person_is_not_made_again(test_db) -> None:
    with get_connection(test_db) as conn:
        video = _item(conn, "youtube", "history:a", "BBC", {"channel_id": "UC1"})
        link_agents(conn)
        conn.execute("DELETE FROM people WHERE name = 'BBC'")
        assert link_agents(conn) == 0
        assert _agents(conn, video) == []
        # The account stays, belonging to no one, so the next pass knows it
        assert [r[0] for r in conn.execute("SELECT person_id FROM person_identities")] == [None]


def test_your_address_is_yours_though_first_seen_as_a_recipient(test_db) -> None:
    with get_connection(test_db) as conn:
        _profile(conn, "Matthew Thompson")
        received = _item(
            conn,
            "email",
            "e1",
            "Ann",
            {"from_email": "ann@example.org", "to_email": "me@example.org"},
        )
        sent = _item(
            conn, "email", "e2", "Matthew Thompson", {"from_email": "me@example.org"}, own=True
        )
        link_agents(conn)
        you = ("author", None, "Matthew Thompson", "person", True)
        assert _agents(conn, sent) == [you]
        assert ("recipient", None, "Matthew Thompson", "person", True) in _agents(conn, received)
        assert (
            conn.execute("SELECT COUNT(*) FROM people WHERE name = 'me@example.org'").fetchone()[0]
            == 0
        )


def test_a_mailing_list_is_a_group_not_its_first_poster(test_db) -> None:
    list_meta = {
        "from_email": "uai-list@googlegroups.com",
        "list_id": "<uai-list.googlegroups.com>",
    }
    with get_connection(test_db) as conn:
        one = _item(conn, "gmail", "g1", "'Stefano' via UAI Mailing List", list_meta)
        two = _item(conn, "gmail", "g2", "'caip2027 .' via UAI Mailing List", list_meta)
        direct = _item(
            conn,
            "gmail",
            "g3",
            "Paola",
            {"from_email": "paola@example.org", "list_id": "<uai-list.googlegroups.com>"},
        )
        link_agents(conn)
        group = ("publisher", None, "UAI Mailing List", "group", False)
        assert _agents(conn, one) == [group]
        assert _agents(conn, two) == [group]
        assert _agents(conn, direct) == [("author", None, "Paola", "person", False), group]


def test_someone_elses_post_and_odd_metadata(test_db) -> None:
    with get_connection(test_db) as conn:
        theirs = _item(
            conn,
            "bluesky",
            "b1",
            "Farrukh",
            {"author_did": "did:plc:f", "author_display_name": "Farrukh"},
        )
        conn.execute(
            "INSERT INTO items (source_type, source_id, content, author, metadata) "
            "VALUES ('youtube', 'bad', 'x', 'A', '{not json'), ('youtube', 'list', 'x', 'A', '[1]')"
        )
        stub = _item(conn, "youtube", "stub:v", "BBC", {"channel_id": "UC1"})
        conn.execute("UPDATE items SET held = 0 WHERE id = ?", (stub,))
        link_agents(conn)
        assert _agents(conn, theirs) == [("author", None, "Farrukh", "person", False)]
        assert _agents(conn, stub) == []
        # Held later: its agents follow
        conn.execute("UPDATE items SET held = 1 WHERE id = ?", (stub,))
        link_agents(conn)
        assert _agents(conn, stub) == [("creator", None, "BBC", "organization", False)]


def test_a_later_pass_claims_your_address_from_an_earlier_one(test_db) -> None:
    with get_connection(test_db) as conn:
        _profile(conn, "Me")
        received = _item(
            conn, "email", "e1", "Ann", {"from_email": "ann@x.org", "to_email": "me@x.org"}
        )
        link_agents(conn)
        assert ("recipient", None, "me@x.org", "person", False) in _agents(conn, received)
        sent = _item(conn, "email", "e2", "Me", {"from_email": "me@x.org"}, own=True)
        link_agents(conn)
        assert _agents(conn, sent) == [("author", None, "Me", "person", True)]
        assert ("recipient", None, "Me", "person", True) in _agents(conn, received)
        assert (
            conn.execute("SELECT COUNT(*) FROM people WHERE name = 'me@x.org'").fetchone()[0] == 0
        )


def test_a_named_person_given_an_account_is_not_made_again(test_db) -> None:
    with get_connection(test_db) as conn:
        paper = _item(conn, "arxiv", "2401.1", None, {"authors": ["Ann Lee"]})
        link_agents(conn)
        [ann] = agents_for(conn, paper)
        conn.execute(
            "INSERT INTO person_identities (person_id, platform, identifier, status, origin) "
            "VALUES (?, 'orcid', '0000-1', 'suggested', 'linker:name')",
            (ann["person"]["id"],),
        )
        assert link_agents(conn) == 0
        assert len(agents_for(conn, paper)) == 1


def test_a_list_is_known_by_its_id_whatever_its_description(test_db) -> None:
    with get_connection(test_db) as conn:
        for n, list_id in enumerate(
            ('"Python-Dev" <python-dev.python.org>', "<Python-Dev.python.org>")
        ):
            _item(conn, "gmail", f"g{n}", "Ann", {"from_email": "a@x.org", "list_id": list_id})
        link_agents(conn)
        rows = conn.execute(
            "SELECT identifier FROM person_identities WHERE platform = 'mailing-list'"
        ).fetchall()
        assert [r[0] for r in rows] == ["python-dev.python.org"]


def test_you_are_not_named_by_a_urn(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(conn, "linkedin", "p1", ME, {"raw": {"owner": ME}})
        link_agents(conn)
        assert conn.execute("SELECT name FROM people WHERE is_self = 1").fetchone()[0] == "You"


def test_an_email_marked_yours_by_default_but_sent_by_another_is_theirs(test_db) -> None:
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO person_profiles (urn, display_name) VALUES (?, 'Matthew Thompson')",
            (ME,),
        )
        _item(conn, "linkedin", "p1", ME, {"raw": {"owner": ME}})
        # A thread imported without is_own_content: every message defaults to yours
        theirs = _item(conn, "email", "e1", "Andrew", {"from_email": "andrew@x.org"}, own=True)
        mine = _item(conn, "email", "e2", "Matthew Thompson", {"from_email": "me@x.org"}, own=True)
        link_agents(conn)
        assert _agents(conn, theirs) == [("author", None, "Andrew", "person", False)]
        assert _agents(conn, mine) == [("author", None, "Matthew Thompson", "person", True)]


def test_no_email_is_yours_until_your_name_is_known(test_db) -> None:
    with get_connection(test_db) as conn:
        mail = _item(conn, "email", "e1", "Ann", {"from_email": "ann@x.org"}, own=True)
        link_agents(conn)
        assert _agents(conn, mail) == [("author", None, "Ann", "person", False)]


def _tagged(text: str, tags: list[tuple[int, int, str, str]]) -> dict[str, Any]:
    """LinkedIn changelog metadata for a comment whose text tags members or companies."""
    kinds = {
        "member": "com.linkedin.common.MemberAttributedEntity",
        "company": "com.linkedin.common.CompanyAttributedEntity",
    }
    attributes = [
        {"start": start, "length": length, "value": {kinds[field]: {field: urn}}}
        for start, length, field, urn in tags
    ]
    return {"raw": {"owner": ME, "activity": {"message": {"text": text, "attributes": attributes}}}}


def test_a_tagged_member_is_a_confirmed_mention_named_as_written(test_db) -> None:
    # The emoji is two UTF-16 units, as LinkedIn counts: "Ray Myers" starts at 3
    text = "🎉 Ray Myers and Acme, see this"
    meta = _tagged(
        text, [(3, 9, "member", "urn:li:person:ray"), (17, 4, "company", "urn:li:company:1")]
    )
    with get_connection(test_db) as conn:
        # Ray was first seen as an author, named by his URN
        theirs = _item(conn, "linkedin", "c0", "urn:li:person:ray", {"raw": {"owner": ME}})
        comment = _item(conn, "linkedin", "c1", ME, meta)
        link_agents(conn)
        assert _agents(conn, comment) == [
            ("author", None, "You", "person", True),
            ("mentioned", None, "Acme", "organization", False),
            ("mentioned", None, "Ray Myers", "person", False),
        ]
        assert _agents(conn, theirs) == [("author", None, "Ray Myers", "person", False)]
        row = conn.execute("SELECT status, evidence, origin FROM item_mentions LIMIT 1").fetchone()
        assert tuple(row) == ("confirmed", "tagged", "import:linkedin")
        assert link_agents(conn) == 0


def test_the_longest_way_a_name_is_written_names_them(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(
            conn,
            "linkedin",
            "c1",
            ME,
            _tagged("Ray thanks", [(0, 3, "member", "urn:li:person:ray")]),
        )
        _item(
            conn,
            "linkedin",
            "c2",
            ME,
            _tagged("Ray Myers yes", [(0, 9, "member", "urn:li:person:ray")]),
        )
        link_agents(conn)
        names = [r[0] for r in conn.execute("SELECT name FROM people WHERE is_self = 0")]
        assert names == ["Ray Myers"]

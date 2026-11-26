"""Agents: the people, channels and software that made, voiced or received each item."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from lestash.core.agents import agents_for, link_agents
from lestash.core.database import (
    SCHEMA_VERSION,
    apply_migrations,
    get_connection,
    upsert_person_profile,
)

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
            "DROP INDEX idx_activities_key; ALTER TABLE activities DROP COLUMN actor_id; "
            "DROP TABLE people;"
        )
        conn.execute("PRAGMA user_version = 18")
        assert apply_migrations(conn) == SCHEMA_VERSION - 18
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
        mine = _item(conn, "linkedin", "p1", ME, {"raw": {"owner": ME}})
        upsert_person_profile(conn, ME, display_name="Matthew Thompson")
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
    _item(conn, "linkedin", f"profile:{name}", ME, {"raw": {"owner": ME}})
    upsert_person_profile(conn, ME, display_name=name)


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
        _item(conn, "linkedin", "p1", ME, {"raw": {"owner": ME}})
        upsert_person_profile(conn, ME, display_name="Matthew Thompson")
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


def test_a_profile_cut_from_a_mention_loses_to_the_right_decode(test_db) -> None:
    text = "🤣 Max Langley see"
    with get_connection(test_db) as conn:
        # Cut by code points, as an old script did: two too many after the emoji
        upsert_person_profile(
            conn, "urn:li:person:max", display_name="Max Langley s", source="mention"
        )
        _item(conn, "linkedin", "c1", ME, _tagged(text, [(3, 11, "member", "urn:li:person:max")]))
        link_agents(conn)
        names = [r[0] for r in conn.execute("SELECT name FROM people WHERE is_self = 0")]
        assert names == ["Max Langley"]


def test_a_better_name_found_later_replaces_the_one_given(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(
            conn,
            "linkedin",
            "c1",
            ME,
            _tagged("Ray thanks", [(0, 3, "member", "urn:li:person:ray")]),
        )
        link_agents(conn)
        _item(
            conn,
            "linkedin",
            "c2",
            ME,
            _tagged("Ray Myers yes", [(0, 9, "member", "urn:li:person:ray")]),
        )
        link_agents(conn)
        assert [r[0] for r in conn.execute("SELECT name FROM people WHERE is_self = 0")] == [
            "Ray Myers"
        ]
        # A name you set is kept
        conn.execute("UPDATE people SET name = 'Raymond' WHERE is_self = 0")
        _item(
            conn,
            "linkedin",
            "c3",
            ME,
            _tagged("Ray Myers Jr yes", [(0, 12, "member", "urn:li:person:ray")]),
        )
        link_agents(conn)
        assert [r[0] for r in conn.execute("SELECT name FROM people WHERE is_self = 0")] == [
            "Raymond"
        ]


def test_offsets_that_split_an_emoji_or_overrun_are_skipped(test_db) -> None:
    with get_connection(test_db) as conn:
        bad = _tagged(
            "🎉Ray",
            [(1, 4, "member", "urn:li:person:a"), (2, 9, "member", "urn:li:person:b")],
        )
        item = _item(conn, "linkedin", "c1", ME, bad)
        link_agents(conn)
        assert [a for a in _agents(conn, item) if a[0] == "mentioned"] == []


def test_a_bluesky_mention_names_its_account_once_a_post_of_theirs_does(test_db) -> None:
    with get_connection(test_db) as conn:
        mine = _item(
            conn,
            "bluesky",
            "b1",
            "Me",
            {
                "author_did": "did:plc:me",
                "author_display_name": "Matthew Thompson",
                "facets": {"mentions": ["did:plc:ann", "did:plc:ann", "not-a-did", 7]},
            },
            own=True,
        )
        link_agents(conn)
        assert _agents(conn, mine) == [
            ("author", None, "Matthew Thompson", "person", True),
            ("mentioned", None, "did:plc:ann", "person", False),
        ]
        _item(
            conn,
            "bluesky",
            "b2",
            "Ann",
            {"author_did": "did:plc:ann", "author_display_name": "Ann"},
        )
        link_agents(conn)
        assert _agents(conn, mine)[1] == ("mentioned", None, "Ann", "person", False)
        assert link_agents(conn) == 0


def _bluesky_url(conn: sqlite3.Connection, did: str) -> str | None:
    return conn.execute(
        "SELECT url FROM person_identities WHERE platform = 'bluesky' AND identifier = ?",
        (did,),
    ).fetchone()[0]


def test_a_bluesky_mention_takes_the_handle_address_once_a_post_of_theirs_gives_it(
    test_db,
) -> None:
    """#379: first seen by a mention, an account has its DID's address; a post of theirs
    gives their handle, whose address replaces it."""
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "Me",
            {"author_did": "did:plc:me", "facets": {"mentions": ["did:plc:zed"]}},
            own=True,
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/did:plc:zed"
        _item(
            conn,
            "bluesky",
            "b2",
            "Zed",
            {
                "author_did": "did:plc:zed",
                "author_handle": "zed.example.social",
                "author_display_name": "Zed Example",
            },
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/zed.example.social"
        # A later mention keeps the handle's address
        _item(
            conn,
            "bluesky",
            "b3",
            "Me",
            {"author_did": "did:plc:me", "facets": {"mentions": ["did:plc:zed"]}},
            own=True,
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/zed.example.social"


def test_a_bluesky_address_set_otherwise_is_kept(test_db) -> None:
    """Only the address made from the DID gives way: one set some other way stays."""
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "Me",
            {"author_did": "did:plc:me", "facets": {"mentions": ["did:plc:zed"]}},
            own=True,
        )
        link_agents(conn)
        conn.execute(
            "UPDATE person_identities SET url = 'https://zed.example.org' "
            "WHERE identifier = 'did:plc:zed'"
        )
        _item(
            conn,
            "bluesky",
            "b2",
            "Zed",
            {"author_did": "did:plc:zed", "author_handle": "zed.example.social"},
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://zed.example.org"


def test_a_post_of_yours_that_mentions_you_names_you(test_db) -> None:
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "Me",
            {
                "author_did": "did:plc:me",
                "author_display_name": "Matthew Thompson",
                "facets": {"mentions": ["did:plc:me"]},
            },
            own=True,
        )
        link_agents(conn)
        assert (
            conn.execute(
                "SELECT display_name FROM person_identities WHERE identifier = 'did:plc:me'"
            ).fetchone()[0]
            == "Matthew Thompson"
        )


def test_a_bluesky_address_follows_the_newest_posts_handle(test_db) -> None:
    """#379, review rounds 2 and 5: an address made from a handle follows a handle
    change; the handle fetched last wins, whatever order the pass reads posts in."""

    def post(conn: sqlite3.Connection, n: str, handle: str, when: str) -> None:
        item = _item(
            conn,
            "bluesky",
            n,
            "Zed",
            {"author_did": "did:plc:zed", "author_handle": handle},
        )
        conn.execute("UPDATE items SET fetched_at = ? WHERE id = ?", (when, item))

    with get_connection(test_db) as conn:
        # The newer post is read first; the older one must not take its place
        post(conn, "b2", "zed-new.example.social", "2026-06-01T10:00:00+00:00")
        post(conn, "b1", "zed-old.example.social", "2025-01-01T10:00:00+00:00")
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == (
            "https://bsky.app/profile/zed-new.example.social"
        )
        post(conn, "b3", "zed-newest.example.social", "2026-11-01T10:00:00+00:00")
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == (
            "https://bsky.app/profile/zed-newest.example.social"
        )


def test_a_bsky_address_you_set_stays_after_a_newer_post(test_db) -> None:
    """#379, review round 3: an address on bsky.app that no post of the account gave
    was set some other way, so a newer post's handle does not replace it."""
    with get_connection(test_db) as conn:
        _item(conn, "bluesky", "b1", "Zed", {"author_did": "did:plc:zed", "author_handle": "a.x"})
        link_agents(conn)
        conn.execute(
            "UPDATE person_identities SET url = 'https://bsky.app/profile/chosen.example' "
            "WHERE identifier = 'did:plc:zed'"
        )
        _item(conn, "bluesky", "b2", "Zed", {"author_did": "did:plc:zed", "author_handle": "b.x"})
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/chosen.example"


def test_an_unverified_bluesky_handle_never_becomes_an_address(test_db) -> None:
    """#379, review round 5: Bluesky's placeholder 'handle.invalid' is not a handle."""
    with get_connection(test_db) as conn:
        _item(conn, "bluesky", "b1", "Zed", {"author_did": "did:plc:zed", "author_handle": "a.x"})
        link_agents(conn)
        _item(
            conn,
            "bluesky",
            "b2",
            "Zed",
            {"author_did": "did:plc:zed", "author_handle": "handle.invalid"},
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/a.x"


def test_a_first_post_with_an_unverified_handle_gives_no_address(test_db) -> None:
    """#379, review round 6: 'handle.invalid' on the first post seen gives no address,
    so a real handle seen later fills it."""
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "Zed",
            {"author_did": "did:plc:zed", "author_handle": "handle.invalid"},
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") is None
        _item(conn, "bluesky", "b2", "Zed", {"author_did": "did:plc:zed", "author_handle": "z.x"})
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/z.x"


def test_a_resynced_posts_new_handle_wins_over_a_later_fetch(test_db) -> None:
    """#379, review round 6: a re-sync that changes an old post's handle keeps its old
    fetched_at but leaves a history row; that change counts as the handle's sighting."""
    with get_connection(test_db) as conn:
        old = _item(
            conn, "bluesky", "b1", "Zed", {"author_did": "did:plc:zed", "author_handle": "a.x"}
        )
        later = _item(
            conn, "bluesky", "b2", "Zed", {"author_did": "did:plc:zed", "author_handle": "a.x"}
        )
        conn.execute("UPDATE items SET fetched_at = '2025-01-01 00:00:00' WHERE id = ?", (old,))
        conn.execute("UPDATE items SET fetched_at = '2026-01-01 00:00:00' WHERE id = ?", (later,))
        # The re-sync of the old post brings the new handle
        conn.execute(
            "UPDATE items SET metadata = ? WHERE id = ?",
            (json.dumps({"author_did": "did:plc:zed", "author_handle": "b.x"}), old),
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/b.x"


def test_only_a_change_of_handle_counts_as_its_sighting(test_db) -> None:
    """#379, review round 7: a later edit that leaves the handle alone (a tag, a title)
    does not make an old handle look newer."""
    with get_connection(test_db) as conn:
        old = _item(
            conn, "bluesky", "b1", "Zed", {"author_did": "did:plc:zed", "author_handle": "a.x"}
        )
        new = _item(
            conn, "bluesky", "b2", "Zed", {"author_did": "did:plc:zed", "author_handle": "b.x"}
        )
        conn.execute("UPDATE items SET fetched_at = '2025-01-01 00:00:00' WHERE id = ?", (old,))
        conn.execute("UPDATE items SET fetched_at = '2026-01-01 00:00:00' WHERE id = ?", (new,))
        conn.execute("UPDATE items SET title = 'edited' WHERE id = ?", (old,))
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/b.x"


def test_an_unverified_handle_never_names_an_account(test_db) -> None:
    """#379, review round 7: with no display name, items.author holds the handle; the
    placeholder 'handle.invalid' is not a name."""
    with get_connection(test_db) as conn:
        item = _item(
            conn,
            "bluesky",
            "b1",
            "handle.invalid",
            {"author_did": "did:plc:zed", "author_handle": "handle.invalid"},
        )
        link_agents(conn)
        assert _agents(conn, item)[0][2] == "did:plc:zed"


def test_an_account_named_by_the_placeholder_handle_is_repaired(test_db) -> None:
    """#379, review round 8: an account made before the fix, with the address and name
    of 'handle.invalid', takes the real handle's address and name."""
    with get_connection(test_db) as conn:
        _item(conn, "bluesky", "b1", "Zed", {"author_did": "did:plc:zed", "author_handle": "z.x"})
        link_agents(conn)
        conn.execute(
            "UPDATE person_identities SET url = 'https://bsky.app/profile/handle.invalid', "
            "display_name = 'handle.invalid' WHERE identifier = 'did:plc:zed'"
        )
        conn.execute(
            "UPDATE people SET name = 'handle.invalid' WHERE id = (SELECT person_id FROM "
            "person_identities WHERE identifier = 'did:plc:zed')"
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/z.x"
        assert (
            conn.execute(
                "SELECT p.name FROM people p JOIN person_identities i ON i.person_id = p.id "
                "WHERE i.identifier = 'did:plc:zed'"
            ).fetchone()[0]
            == "z.x"
        )


def test_a_placeholder_account_with_no_real_handle_falls_back_to_its_did(test_db) -> None:
    """#379, review round 9: with no post giving a real handle, an account named and
    addressed by 'handle.invalid' takes its DID's name and address, as a mention does."""
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "handle.invalid",
            {"author_did": "did:plc:zed", "author_handle": "handle.invalid"},
        )
        link_agents(conn)
        conn.execute(
            "UPDATE person_identities SET url = 'https://bsky.app/profile/handle.invalid', "
            "display_name = 'handle.invalid' WHERE identifier = 'did:plc:zed'"
        )
        conn.execute(
            "UPDATE people SET name = 'handle.invalid' WHERE id = (SELECT person_id FROM "
            "person_identities WHERE identifier = 'did:plc:zed')"
        )
        link_agents(conn)
        assert _bluesky_url(conn, "did:plc:zed") == "https://bsky.app/profile/did:plc:zed"
        assert (
            conn.execute(
                "SELECT p.name FROM people p JOIN person_identities i ON i.person_id = p.id "
                "WHERE i.identifier = 'did:plc:zed'"
            ).fetchone()[0]
            == "did:plc:zed"
        )


def test_your_account_named_by_the_placeholder_handle_is_repaired(test_db) -> None:
    """#379, review round 10: your own account is repaired too."""
    with get_connection(test_db) as conn:
        _item(
            conn,
            "bluesky",
            "b1",
            "Me",
            {"author_did": "did:plc:me", "author_handle": "me.x"},
            own=True,
        )
        link_agents(conn)
        conn.execute(
            "UPDATE person_identities SET display_name = 'handle.invalid' "
            "WHERE identifier = 'did:plc:me'"
        )
        conn.execute("UPDATE people SET name = 'handle.invalid' WHERE is_self = 1")
        link_agents(conn)
        assert conn.execute("SELECT name FROM people WHERE is_self = 1").fetchone()[0] == "me.x"
        assert (
            conn.execute(
                "SELECT display_name FROM person_identities WHERE identifier = 'did:plc:me'"
            ).fetchone()[0]
            == "me.x"
        )


def test_agents_are_found_with_no_write_open(test_db, monkeypatch) -> None:
    """The scan of every item runs before the write lock is taken (#416)."""
    from lestash.core import agents

    real = agents.agents_of
    seen: list[bool] = []

    def spy(conn: sqlite3.Connection, ctx: Any = None) -> Any:
        seen.append(conn.in_transaction)
        return real(conn, ctx)

    monkeypatch.setattr(agents, "agents_of", spy)
    with get_connection(test_db) as conn:
        _item(conn, "youtube", "history:a", "BBC News", {"channel_id": "UC1"})
        conn.commit()
        assert link_agents(conn) == 1
    assert seen == [False]


def test_agents_are_found_again_when_another_writer_changed_items(test_db, monkeypatch) -> None:
    """A write by another connection between the scan and the lock is not missed (#416)."""
    from lestash.core import agents

    real = agents.agents_of
    calls: list[bool] = []

    def found_then_changed(conn: sqlite3.Connection, ctx: Any = None) -> Any:
        found = list(real(conn, ctx))
        if not calls:
            with get_connection(test_db) as other:
                other.execute("DELETE FROM items WHERE source_id = 'history:gone'")
                other.execute(
                    "UPDATE items SET metadata = ? WHERE source_id = 'history:kept'",
                    (json.dumps({"channel_id": "UC2", "channel_title": "Other"}),),
                )
                other.commit()
        calls.append(conn.in_transaction)
        return iter(found)

    monkeypatch.setattr(agents, "agents_of", found_then_changed)
    with get_connection(test_db) as conn:
        kept = _item(conn, "youtube", "history:kept", "Other", {"channel_id": "UC1"})
        _item(conn, "youtube", "history:gone", "BBC News", {"channel_id": "UC1"})
        conn.commit()
        assert link_agents(conn) == 1
        assert calls == [False, True]  # found again, under the lock
        [agent] = agents_for(conn, kept)
        assert agent["accounts"][0]["identifier"] == "UC2"

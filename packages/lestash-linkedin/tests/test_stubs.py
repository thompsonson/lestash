"""Stubs for the LinkedIn posts your comments and reactions are on, when not held."""

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_linkedin.source import resolve_linkedin_parents
from lestash_linkedin.stubs import link_linkedin_targets, post_urn, snowflake_ms

ME = "urn:li:person:me"
POST = "urn:li:activity:7418912385886941184"  # 2026-01-19T07:09:09.522Z
ON_COMMENT = "urn:li:comment:(activity:7418912385886941184,7418939597658832896)"


@pytest.fixture
def test_db() -> Iterator[Config]:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(general=GeneralConfig(database_path=str(Path(tmpdir) / "t.db")))
        init_database(config)
        yield config


def _comment(n: int, on: str = POST, actor: str = ME) -> ItemCreate:
    return ItemCreate(
        source_type="linkedin",
        source_id=f"changelog-socialActions/comments-{n}",
        content=f"Comment {n}",
        created_at="2026-01-19 10:45:54.022000",
        metadata={"commented_on": on, "raw": {"actor": actor, "owner": ME}},
    )


def _reaction(n: int, on: str, actor: str = ME, time_ms: int | None = 1768815923678) -> ItemCreate:
    raw: dict = {"actor": actor, "owner": ME}
    if time_ms:
        raw["activity"] = {"created": {"time": time_ms}}
    return ItemCreate(
        source_type="linkedin",
        source_id=f"changelog-socialActions/likes-{n}",
        content="👏 PRAISE",
        created_at="2026-01-19 10:45:54.022000",
        metadata={"reacted_to": on, "reaction_type": "PRAISE", "raw": raw},
    )


def _post() -> ItemCreate:
    return ItemCreate(
        source_type="linkedin",
        source_id="changelog-ugcPosts-urn:li:share:1",
        content="The post itself",
        metadata={"resource_name": "ugcPosts", "post_id": POST},
    )


def _rows(conn, sql: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql)]


def _stubs(conn) -> dict[str, int]:
    return {r[1]: r[0] for r in conn.execute("SELECT id, source_id FROM items WHERE held = 0")}


def _post_with(post_id: str, n: int = 1) -> ItemCreate:
    return ItemCreate(
        source_type="linkedin",
        source_id=f"changelog-ugcPosts-{n}",
        content="A post",
        metadata={"resource_name": "ugcPosts", "post_id": post_id},
    )


def test_urns_name_their_post_and_its_time() -> None:
    assert post_urn(ON_COMMENT) == POST
    assert post_urn("urn:li:comment:(ugcPost:7,8)") == "urn:li:ugcPost:7"
    assert post_urn(POST) == POST
    assert snowflake_ms(POST) == 1768806549522
    assert snowflake_ms("urn:li:groupPost:15875004-7418912385886941184") == 1768806549522
    assert snowflake_ms("urn:li:groupPost:abc") is None


def test_comments_on_a_post_not_held_reply_to_its_stub(test_db) -> None:
    with get_connection(test_db) as conn:
        for item in (
            _comment(1),
            _comment(2, actor="urn:li:person:other"),
            _comment(3, ON_COMMENT),
        ):
            upsert_item(conn, item)
        resolve_linkedin_parents(conn)
        stubs = _stubs(conn)
        assert set(stubs) == {f"stub:{POST}", f"stub:{ON_COMMENT}"}
        post_stub, comment_stub = stubs[f"stub:{POST}"], stubs[f"stub:{ON_COMMENT}"]
        url, created_at, content = conn.execute(
            "SELECT url, created_at, content FROM items WHERE id = ?", (post_stub,)
        ).fetchone()
        assert url == f"https://www.linkedin.com/feed/update/{POST}"
        assert created_at == "2026-01-19T07:09:09.522000+00:00"
        assert content == ""
        links = _rows(
            conn, "SELECT to_id, kind, evidence, origin, status FROM item_links ORDER BY from_id"
        )
        assert links == [
            (post_stub, "in_reply_to", POST, "import:linkedin", "confirmed"),
            (post_stub, "in_reply_to", POST, "import:linkedin", "confirmed"),
            (comment_stub, "in_reply_to", ON_COMMENT, "import:linkedin", "confirmed"),
        ]
        # The comments stay top-level, so listings still show them
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE parent_id IS NOT NULL") == [(0,)]
        # Again, nothing new
        assert tuple(link_linkedin_targets(conn)) == (0, 0, 0, 0)


def test_your_reactions_are_liked_activities_and_others_wait(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _reaction(1, ON_COMMENT))
        upsert_item(conn, _reaction(2, POST, time_ms=None))
        upsert_item(conn, _reaction(3, POST, actor="urn:li:person:other"))
        deleted = _reaction(4, POST, time_ms=1768815999999)
        assert deleted.metadata is not None
        deleted.metadata["method"] = "DELETE"
        upsert_item(conn, deleted)
        resolve_linkedin_parents(conn)
        stubs = _stubs(conn)
        activities = _rows(
            conn, "SELECT verb, item_id, occurred_at, source, details FROM activities ORDER BY id"
        )
    praise = json.dumps({"reaction": "PRAISE"})
    assert activities == [
        # A like on a comment is on the comment, not its post
        ("liked", stubs[f"stub:{ON_COMMENT}"], "2026-01-19T09:45:23.678Z", "linkedin", praise),
        # Without LinkedIn's own time, created_at is read as this machine's local time
        ("liked", stubs[f"stub:{POST}"], activities[1][2], "linkedin", praise),
    ]


def test_an_imported_post_takes_over_its_stub_and_what_was_attached(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _comment(1))
        upsert_item(conn, _reaction(1, POST))
        resolve_linkedin_parents(conn)
        [stub] = _stubs(conn).values()
        note = conn.execute(
            "INSERT INTO items (source_type, content, parent_id) VALUES ('note', 'n', ?) "
            "RETURNING id",
            (stub,),
        ).fetchone()[0]
        tag = conn.execute("INSERT INTO tags (name) VALUES ('t') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (stub, tag))

        post = upsert_item(conn, _post())
        resolve_linkedin_parents(conn)
        assert _stubs(conn) == {}
        # The comment is now the post's child, and keeps its link to the post
        assert _rows(conn, "SELECT to_id, kind FROM item_links") == [(post, "in_reply_to")]
        assert _rows(conn, "SELECT item_id, verb FROM activities") == [(post, "liked")]
        children = _rows(conn, f"SELECT id FROM items WHERE parent_id = {post} ORDER BY id")
        assert note in [c[0] for c in children] and len(children) == 3
        assert _rows(conn, "SELECT item_id FROM item_tags") == [(post,)]
        # Again: no second like, no new stub
        assert tuple(link_linkedin_targets(conn)) == (0, 0, 0, 0)


def test_a_post_named_by_another_urn_made_with_it_is_the_parent(test_db) -> None:
    share = 7418912385886941184 - (500 << 22)  # made 0.5 s before the activity
    with get_connection(test_db) as conn:
        post = upsert_item(conn, _post_with(f"urn:li:share:{share}"))
        upsert_item(conn, _post_with(f"urn:li:share:{share}", n=2))  # held twice
        upsert_item(conn, _comment(1))
        upsert_item(conn, _reaction(1, POST))
        resolve_linkedin_parents(conn)
        assert _stubs(conn) == {}
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE parent_id = " + str(post)) == [(2,)]
        assert _rows(conn, "SELECT item_id FROM activities") == [(post,)]
        assert _rows(conn, "SELECT COUNT(*) FROM item_links") == [(0,)]


def test_a_post_made_seconds_apart_is_another_post(test_db) -> None:
    other = 7418912385886941184 - (3000 << 22)  # 3 s earlier
    with get_connection(test_db) as conn:
        upsert_item(conn, _post_with(f"urn:li:share:{other}"))
        upsert_item(conn, _comment(1))
        resolve_linkedin_parents(conn)
        assert set(_stubs(conn)) == {f"stub:{POST}"}
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE parent_id IS NOT NULL") == [(0,)]


def test_a_reaction_on_a_held_post_is_an_activity_on_it(test_db) -> None:
    with get_connection(test_db) as conn:
        post = upsert_item(conn, _post())
        upsert_item(conn, _reaction(1, POST))
        resolve_linkedin_parents(conn)
        assert _rows(conn, "SELECT item_id FROM activities") == [(post,)]
        assert _stubs(conn) == {}

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
from lestash_linkedin.stubs import link_linkedin_targets, post_urn

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


def test_post_urn_of_a_comment_is_the_post_it_is_on() -> None:
    assert post_urn(ON_COMMENT) == POST
    assert post_urn("urn:li:comment:(ugcPost:7,8)") == "urn:li:ugcPost:7"
    assert post_urn(POST) == POST


def test_comments_on_a_post_not_held_reply_to_one_stub(test_db) -> None:
    with get_connection(test_db) as conn:
        for item in (
            _comment(1),
            _comment(2, actor="urn:li:person:other"),
            _comment(3, ON_COMMENT),
        ):
            upsert_item(conn, item)
        resolve_linkedin_parents(conn)
        [(stub, url, created_at, content)] = _rows(
            conn, "SELECT id, url, created_at, content FROM items WHERE held = 0"
        )
        assert url == f"https://www.linkedin.com/feed/update/{POST}"
        assert created_at == "2026-01-19T07:09:09.522000+00:00"
        assert content == ""
        links = _rows(
            conn, "SELECT to_id, kind, evidence, origin, status FROM item_links ORDER BY from_id"
        )
        assert links == [
            (stub, "in_reply_to", POST, "import:linkedin", "confirmed"),
            (stub, "in_reply_to", POST, "import:linkedin", "confirmed"),
            (stub, "in_reply_to", ON_COMMENT, "import:linkedin", "confirmed"),
        ]
        # The comments stay top-level, so listings still show them
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE parent_id IS NOT NULL") == [(0,)]
        # Again, nothing new
        assert tuple(link_linkedin_targets(conn)) == (0, 0, 0)


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
        [(stub,)] = _rows(conn, "SELECT id FROM items WHERE held = 0")
        activities = _rows(
            conn, "SELECT verb, item_id, occurred_at, source, details FROM activities ORDER BY id"
        )
    assert activities == [
        (
            "liked",
            stub,
            "2026-01-19T09:45:23.678Z",
            "linkedin",
            json.dumps({"reaction": "PRAISE", "on": ON_COMMENT}),
        ),
        # Without LinkedIn's own time, created_at is read as this machine's local time
        ("liked", stub, activities[1][2], "linkedin", json.dumps({"reaction": "PRAISE"})),
    ]


def test_an_imported_post_takes_over_its_stub(test_db) -> None:
    with get_connection(test_db) as conn:
        upsert_item(conn, _comment(1))
        upsert_item(conn, _reaction(1, POST))
        resolve_linkedin_parents(conn)
        [(stub,)] = _rows(conn, "SELECT id FROM items WHERE held = 0")

        post = upsert_item(conn, _post())
        resolve_linkedin_parents(conn)
        assert _rows(conn, "SELECT id FROM items WHERE id = " + str(stub)) == []
        # The comment is now the post's child, and keeps its link to the post
        assert _rows(conn, "SELECT to_id, kind FROM item_links") == [(post, "in_reply_to")]
        assert _rows(conn, "SELECT item_id, verb FROM activities") == [(post, "liked")]
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE parent_id = " + str(post)) == [(2,)]


def test_a_reaction_on_a_held_post_is_an_activity_on_it(test_db) -> None:
    with get_connection(test_db) as conn:
        post = upsert_item(conn, _post())
        upsert_item(conn, _reaction(1, POST))
        resolve_linkedin_parents(conn)
        assert _rows(conn, "SELECT item_id FROM activities") == [(post,)]
        assert _rows(conn, "SELECT COUNT(*) FROM items WHERE held = 0") == [(0,)]

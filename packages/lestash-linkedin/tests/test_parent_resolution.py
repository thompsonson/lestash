"""Tests for LinkedIn parent-child relationship resolution."""

import tempfile
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database, upsert_item
from lestash.models.item import ItemCreate
from lestash_linkedin.source import resolve_linkedin_parents


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)
        yield config


@pytest.fixture
def db_with_post(test_db):
    """Database with a LinkedIn post inserted."""
    post = ItemCreate(
        source_type="linkedin",
        source_id="linkedin:ugcPosts:abc123",
        content="Test post content",
        author="urn:li:person:abc",
        metadata={
            "resource_name": "ugcPosts",
            "post_id": "urn:li:activity:7420083185738424320",
        },
    )
    with get_connection(test_db) as conn:
        upsert_item(conn, post)
    return test_db


class TestResolveLinkedinParents:
    """Test resolve_linkedin_parents() function."""

    def test_resolves_reaction_via_post_id(self, db_with_post):
        """Reaction with reacted_to matching a post's metadata.post_id."""
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:reaction:xyz",
            content="👍 LIKE",
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:7420083185738424320",
            },
        )
        with get_connection(db_with_post) as conn:
            upsert_item(conn, reaction)
            updated = resolve_linkedin_parents(conn)

            assert updated == 1
            row = conn.execute(
                "SELECT parent_id FROM items WHERE source_id = ?",
                ("linkedin:reaction:xyz",),
            ).fetchone()
            assert row[0] is not None

    def test_resolves_comment_via_post_id(self, db_with_post):
        """Comment with commented_on matching a post's metadata.post_id."""
        comment = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:comment:xyz",
            content="Great post!",
            metadata={
                "resource_name": "socialActions/comments",
                "commented_on": "urn:li:activity:7420083185738424320",
            },
        )
        with get_connection(db_with_post) as conn:
            upsert_item(conn, comment)
            updated = resolve_linkedin_parents(conn)

            assert updated == 1
            row = conn.execute(
                "SELECT parent_id FROM items WHERE source_id = ?",
                ("linkedin:comment:xyz",),
            ).fetchone()
            assert row[0] is not None

    def test_resolves_via_source_id(self, test_db):
        """Resolution works when parent URN matches source_id directly."""
        post = ItemCreate(
            source_type="linkedin",
            source_id="urn:li:activity:999",
            content="Post via snapshot",
            metadata={"resource_name": "ugcPosts"},
        )
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:reaction:r999",
            content="👍 LIKE",
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:999",
            },
        )
        with get_connection(test_db) as conn:
            upsert_item(conn, post)
            upsert_item(conn, reaction)
            updated = resolve_linkedin_parents(conn)

            assert updated == 1
            row = conn.execute(
                "SELECT parent_id FROM items WHERE source_id = ?",
                ("linkedin:reaction:r999",),
            ).fetchone()
            # parent_id should point to the post
            post_id = conn.execute(
                "SELECT id FROM items WHERE source_id = ?",
                ("urn:li:activity:999",),
            ).fetchone()[0]
            assert row[0] == post_id

    def test_unresolvable_parent_stays_null(self, test_db):
        """Reaction whose parent post is not in DB keeps parent_id NULL."""
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:reaction:orphan",
            content="👍 LIKE",
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:nonexistent",
            },
        )
        with get_connection(test_db) as conn:
            upsert_item(conn, reaction)
            updated = resolve_linkedin_parents(conn)

            assert updated == 0
            row = conn.execute(
                "SELECT parent_id FROM items WHERE source_id = ?",
                ("linkedin:reaction:orphan",),
            ).fetchone()
            assert row[0] is None

    def test_skips_already_resolved(self, db_with_post):
        """Items with parent_id already set are not updated."""
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:reaction:already",
            content="👍 LIKE",
            parent_id=999,
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:7420083185738424320",
            },
        )
        with get_connection(db_with_post) as conn:
            upsert_item(conn, reaction)
            updated = resolve_linkedin_parents(conn)

            assert updated == 0

    def test_resolves_both_reactions_and_comments(self, db_with_post):
        """Both reactions and comments are resolved in one call."""
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:reaction:r1",
            content="👍 LIKE",
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:7420083185738424320",
            },
        )
        comment = ItemCreate(
            source_type="linkedin",
            source_id="linkedin:comment:c1",
            content="Nice!",
            metadata={
                "resource_name": "socialActions/comments",
                "commented_on": "urn:li:activity:7420083185738424320",
            },
        )
        with get_connection(db_with_post) as conn:
            upsert_item(conn, reaction)
            upsert_item(conn, comment)
            updated = resolve_linkedin_parents(conn)

            assert updated == 2

    def test_resolves_via_snowflake_ts(self, test_db):
        """Reaction targeting activity URN resolves to post with share URN
        via matching snowflake_ts."""
        # Real URN pair: share and activity have same second-precision timestamp
        post = ItemCreate(
            source_type="linkedin",
            source_id="changelog-ugcPosts-share123",
            content="Post with share URN",
            metadata={
                "resource_name": "ugcPosts",
                "post_id": "urn:li:share:7441773333693493249",
                "snowflake_ts": 887128512,
            },
        )
        reaction = ItemCreate(
            source_type="linkedin",
            source_id="changelog-socialActions/likes-reaction456",
            content="👍 LIKE",
            metadata={
                "resource_name": "socialActions/likes",
                "reacted_to": "urn:li:activity:7441773334410719232",
                "target_snowflake_ts": 887128512,
            },
        )
        with get_connection(test_db) as conn:
            post_id = upsert_item(conn, post)
            upsert_item(conn, reaction)
            updated = resolve_linkedin_parents(conn)

            assert updated == 1
            row = conn.execute(
                "SELECT parent_id FROM items WHERE source_id = ?",
                ("changelog-socialActions/likes-reaction456",),
            ).fetchone()
            assert row[0] == post_id


class TestUpsertItemParentId:
    """Test that upsert_item correctly handles parent_id."""

    def test_inserts_with_parent_id(self, test_db):
        """ItemCreate with parent_id set is persisted."""
        parent = ItemCreate(
            source_type="test",
            source_id="parent:1",
            content="Parent item",
        )
        with get_connection(test_db) as conn:
            parent_id = upsert_item(conn, parent)

            child = ItemCreate(
                source_type="test",
                source_id="child:1",
                content="Child item",
                parent_id=parent_id,
            )
            child_db_id = upsert_item(conn, child)

            row = conn.execute(
                "SELECT parent_id FROM items WHERE id = ?", (child_db_id,)
            ).fetchone()
            assert row[0] == parent_id

    def test_inserts_without_parent_id(self, test_db):
        """ItemCreate without parent_id stores NULL."""
        item = ItemCreate(
            source_type="test",
            source_id="solo:1",
            content="Standalone item",
        )
        with get_connection(test_db) as conn:
            item_id = upsert_item(conn, item)
            row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (item_id,)).fetchone()
            assert row[0] is None

    def test_upsert_updates_parent_id(self, test_db):
        """Re-upserting with parent_id updates the existing row."""
        item = ItemCreate(
            source_type="test",
            source_id="updateable:1",
            content="Item",
        )
        with get_connection(test_db) as conn:
            item_id = upsert_item(conn, item)

            # Now upsert again with parent_id
            item_with_parent = ItemCreate(
                source_type="test",
                source_id="updateable:1",
                content="Item",
                parent_id=42,
            )
            upsert_item(conn, item_with_parent)

            row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (item_id,)).fetchone()
            assert row[0] == 42

"""Tests for items display logic (_get_author_actor, _get_preview)."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import (
    get_connection,
    init_database,
    upsert_post_cache,
)
from lestash.models.item import Item


@pytest.fixture
def test_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)
        yield config


def make_item(
    id: int = 1,
    content: str = "Test content",
    metadata: dict | None = None,
    author: str | None = None,
    title: str | None = None,
) -> Item:
    """Create a mock Item for testing."""
    return Item(
        id=id,
        source_type="linkedin",
        source_id=f"test-{id}",
        url=None,
        title=title,
        content=content,
        author=author,
        created_at=datetime.now(),
        fetched_at=datetime.now(),
        is_own_content=True,
        metadata=metadata,
    )


class TestGetAuthorActor:
    """Test _get_author_actor function."""

    def test_returns_dash_when_no_metadata(self, test_db):
        """Items without metadata should return dash for both."""
        from lestash.cli.items import _get_author_actor

        item = make_item(metadata=None)
        with get_connection(test_db) as conn:
            author, actor = _get_author_actor(conn, item)

        # Falls back to item.author which is None
        assert author == "-"
        assert actor == "-"

    def test_returns_you_when_owner_differs_from_actor(self, test_db):
        """When owner != actor, someone else reacted to your content."""
        from lestash.cli.items import _get_author_actor

        item = make_item(
            metadata={
                "reacted_to": "urn:li:activity:123",
                "raw": {
                    "owner": "urn:li:person:you",
                    "actor": "urn:li:person:someone_else",
                },
            }
        )
        with get_connection(test_db) as conn:
            author, actor = _get_author_actor(conn, item)

        assert author == "You"
        # Actor URN not in person_profiles, returns full URN
        assert actor == "urn:li:person:someone_else"

    def test_returns_cached_author_when_enriched(self, test_db):
        """When reaction is enriched, show cached author name."""
        from lestash.cli.items import _get_author_actor

        item = make_item(
            metadata={
                "reacted_to": "urn:li:activity:456",
                "raw": {
                    "owner": "urn:li:person:you",
                    "actor": "urn:li:person:you",
                },
            }
        )

        with get_connection(test_db) as conn:
            # Add enrichment data
            upsert_post_cache(
                conn,
                urn="urn:li:activity:456",
                author_name="Patrick Debois",
                source="manual",
            )

            author, actor = _get_author_actor(conn, item)

        assert author == "Patrick Debois"
        assert actor == "urn:li:person:you"

    def test_returns_dash_when_not_enriched(self, test_db):
        """When reaction is not enriched, show dash for author."""
        from lestash.cli.items import _get_author_actor

        item = make_item(
            metadata={
                "reacted_to": "urn:li:activity:789",
                "raw": {
                    "owner": "urn:li:person:you",
                    "actor": "urn:li:person:you",
                },
            }
        )

        with get_connection(test_db) as conn:
            author, actor = _get_author_actor(conn, item)

        assert author == "-"
        assert actor == "urn:li:person:you"

    def test_non_reaction_falls_back_to_item_author(self, test_db):
        """Non-reaction items use item.author field."""
        from lestash.cli.items import _get_author_actor

        item = make_item(
            metadata={"resource_name": "ugcPosts"},
            author="urn:li:person:author123",
        )

        with get_connection(test_db) as conn:
            author, actor = _get_author_actor(conn, item)

        # No reacted_to, so falls back to item.author
        assert author == "urn:li:person:author123"
        assert actor == "-"


class TestGetPreview:
    """Test _get_preview function."""

    def test_returns_content_when_no_enrichment(self, test_db):
        """Without enrichment, show original content."""
        from lestash.cli.items import _get_preview

        item = make_item(content="👍 LIKE on activity:123")

        with get_connection(test_db) as conn:
            preview = _get_preview(conn, item)

        assert preview == "👍 LIKE on activity:123"

    def test_returns_enriched_content_preview(self, test_db):
        """With enrichment, show cached content."""
        from lestash.cli.items import _get_preview

        item = make_item(
            content="👍 LIKE on activity:456",
            metadata={"reacted_to": "urn:li:activity:456"},
        )

        with get_connection(test_db) as conn:
            upsert_post_cache(
                conn,
                urn="urn:li:activity:456",
                content_preview="This is the original post content",
                source="manual",
            )

            preview = _get_preview(conn, item)

        assert '👍 LIKE: "This is the original post content"' in preview

    def test_adds_comment_indicator_for_comment_reactions(self, test_db):
        """Comment reactions should show (comment) indicator."""
        from lestash.cli.items import _get_preview

        item = make_item(
            content="😂 ENTERTAINMENT on (activity:123,456)",
            metadata={"reacted_to": "urn:li:comment:(activity:123,456)"},
        )

        with get_connection(test_db) as conn:
            preview = _get_preview(conn, item)

        assert "(comment)" in preview

    def test_truncates_long_content(self, test_db):
        """Long content should be truncated."""
        from lestash.cli.items import _get_preview

        long_content = "A" * 100
        item = make_item(content=long_content)

        with get_connection(test_db) as conn:
            preview = _get_preview(conn, item, max_length=50)

        assert len(preview) <= 53  # 50 + "..."
        assert preview.endswith("...")

    def test_uses_title_when_available(self, test_db):
        """Items with title should show title."""
        from lestash.cli.items import _get_preview

        item = make_item(
            title="My Article Title",
            content="Full article content here...",
        )

        with get_connection(test_db) as conn:
            preview = _get_preview(conn, item)

        assert preview == "My Article Title"

    def test_shows_reactor_name_when_cached(self, test_db):
        """When reactor_name is cached, show 'from Name' format."""
        from lestash.cli.items import _get_preview

        item = make_item(
            content="👍 LIKE on activity:789",
            metadata={"reacted_to": "urn:li:activity:789"},
        )

        with get_connection(test_db) as conn:
            upsert_post_cache(
                conn,
                urn="urn:li:activity:789",
                content_preview="Your original post",
                reactor_name="Mike",
                source="manual",
            )

            preview = _get_preview(conn, item)

        assert "from Mike" in preview

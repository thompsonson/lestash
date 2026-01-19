"""LinkedIn API Schema Documentation Tests.

These tests document the undocumented LinkedIn Changelog API schema.
Since LinkedIn doesn't provide public documentation for this API,
these "tests" serve as executable documentation that verifies our
understanding of the API structure remains consistent.

They are NOT testing code logic - they document:
- Known resource types (ugcPosts, socialActions/comments, etc.)
- Expected activity structures for each resource type
- Field names and nested paths discovered through API exploration

If these tests fail, it likely means:
1. Someone accidentally removed a schema mapping, OR
2. LinkedIn changed their API (rare but possible)

These tests are intentionally separate from unit tests in the parent
directory to make the distinction clear.
"""

from lestash_linkedin.schemas.content_types import (
    RESOURCE_SCHEMAS,
    CommentActivity,
    InvitationActivity,
    ReactionActivity,
    UgcPostActivity,
)
from pydantic import BaseModel


class TestLinkedInResourceTypes:
    """Document known LinkedIn Changelog API resource types.

    LinkedIn's Changelog API returns events with a `resourceName` field
    that identifies the type of content. These tests document the resource
    types we've discovered and their corresponding Pydantic schemas.

    Resource types discovered through API exploration:
    - ugcPosts: User-generated content posts (text, images, videos)
    - socialActions/comments: Comments on posts
    - socialActions/likes: Reactions (like, celebrate, support, etc.)
    - invitations: Connection invitations
    """

    def test_ugc_posts_resource_is_mapped(self):
        """Document: ugcPosts resource type exists and maps to UgcPostActivity.

        ugcPosts represent user-created posts including:
        - Text posts
        - Image posts
        - Video posts
        - Article shares

        Activity structure documented in UgcPostActivity schema.
        """
        assert "ugcPosts" in RESOURCE_SCHEMAS
        assert RESOURCE_SCHEMAS["ugcPosts"] == UgcPostActivity

    def test_comments_resource_is_mapped(self):
        """Document: socialActions/comments resource type maps to CommentActivity.

        Comments represent user comments on any LinkedIn content.
        The 'socialActions/' prefix indicates these are social interactions.

        Activity structure documented in CommentActivity schema.
        """
        assert "socialActions/comments" in RESOURCE_SCHEMAS
        assert RESOURCE_SCHEMAS["socialActions/comments"] == CommentActivity

    def test_likes_resource_is_mapped(self):
        """Document: socialActions/likes resource type maps to ReactionActivity.

        Despite the name 'likes', this includes all reaction types:
        - LIKE, CELEBRATE, SUPPORT, LOVE, INSIGHTFUL, FUNNY

        Activity structure documented in ReactionActivity schema.
        """
        assert "socialActions/likes" in RESOURCE_SCHEMAS
        assert RESOURCE_SCHEMAS["socialActions/likes"] == ReactionActivity

    def test_invitations_resource_is_mapped(self):
        """Document: invitations resource type maps to InvitationActivity.

        Invitations represent connection requests sent or received.

        Activity structure documented in InvitationActivity schema.
        """
        assert "invitations" in RESOURCE_SCHEMAS
        assert RESOURCE_SCHEMAS["invitations"] == InvitationActivity


class TestSchemaRegistryIntegrity:
    """Verify the schema registry is well-formed.

    These tests ensure that the RESOURCE_SCHEMAS registry maintains
    its structural integrity - all values should be valid Pydantic
    models that can parse LinkedIn API responses.
    """

    def test_all_registered_schemas_are_pydantic_models(self):
        """Verify all registry values inherit from Pydantic BaseModel.

        This ensures we can use model_validate() on any schema in the registry.
        """
        for resource_name, schema_class in RESOURCE_SCHEMAS.items():
            assert issubclass(schema_class, BaseModel), (
                f"{resource_name} schema ({schema_class.__name__}) " "is not a Pydantic BaseModel"
            )

    def test_expected_resource_types_are_all_present(self):
        """Verify all known resource types are registered.

        Uses subset check rather than exact match - additional resource
        types can be added without breaking this test.
        """
        expected_types = {
            "ugcPosts",
            "socialActions/comments",
            "socialActions/likes",
            "invitations",
        }
        assert expected_types.issubset(
            RESOURCE_SCHEMAS.keys()
        ), f"Missing resource types: {expected_types - RESOURCE_SCHEMAS.keys()}"

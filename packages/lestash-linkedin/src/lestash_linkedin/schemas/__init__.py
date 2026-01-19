"""LinkedIn API schemas.

Pydantic models documenting the expected structure of LinkedIn API responses.
When LinkedIn changes their API, tests using these schemas will fail, prompting updates.
"""

from lestash_linkedin.schemas.changelog import ChangelogEvent
from lestash_linkedin.schemas.content_types import (
    RESOURCE_SCHEMAS,
    CommentActivity,
    InvitationActivity,
    ReactionActivity,
    ShareCommentary,
    ShareContent,
    UgcPostActivity,
)

__all__ = [
    "ChangelogEvent",
    "CommentActivity",
    "InvitationActivity",
    "ReactionActivity",
    "RESOURCE_SCHEMAS",
    "ShareCommentary",
    "ShareContent",
    "UgcPostActivity",
]

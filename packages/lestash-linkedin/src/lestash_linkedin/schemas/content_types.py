"""LinkedIn content type schemas for changelog activities.

Each resource type (ugcPosts, socialActions/comments, etc.) has its own
activity structure. These schemas document what we expect and extract.

When LinkedIn changes their API schema, tests using these models will fail,
prompting updates to match the new structure.
"""

from pydantic import BaseModel, Field


class ShareCommentary(BaseModel):
    """Text content of a post.

    Part of the com.linkedin.ugc.ShareContent structure.
    """

    text: str = ""
    attributes: list = Field(default_factory=list)


class ShareContent(BaseModel):
    """Content of a UGC post (com.linkedin.ugc.ShareContent).

    This is the inner content structure for ugcPosts, containing
    the actual post text and media information.
    """

    share_commentary: ShareCommentary = Field(
        default_factory=ShareCommentary, alias="shareCommentary"
    )
    share_media_category: str | None = Field(default=None, alias="shareMediaCategory")
    media: list[dict] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class UgcPostActivity(BaseModel):
    """Activity data for ugcPosts resource.

    Represents a LinkedIn post created by the user.

    The actual post text is nested within:
    specificContent["com.linkedin.ugc.ShareContent"]["shareCommentary"]["text"]
    """

    author: str | None = None  # URN (e.g., "urn:li:person:abc123")
    lifecycle_state: str | None = Field(default=None, alias="lifecycleState")
    visibility: dict | None = None
    specific_content: dict = Field(default_factory=dict, alias="specificContent")
    created: dict | None = None  # {"time": epoch_ms, "actor": urn}
    id: str | None = None  # URN (e.g., "urn:li:share:123456")

    model_config = {"populate_by_name": True}

    def get_text(self) -> str:
        """Extract post text from specificContent.

        Returns:
            The post text, or empty string if not found.
        """
        share_content = self.specific_content.get("com.linkedin.ugc.ShareContent", {})
        commentary = share_content.get("shareCommentary", {})
        return commentary.get("text", "")

    def get_created_at(self) -> int | None:
        """Get creation timestamp in epoch milliseconds.

        Returns:
            Creation time as epoch milliseconds, or None if not available.
        """
        if self.created:
            return self.created.get("time")
        return None


class CommentActivity(BaseModel):
    """Activity data for socialActions/comments resource.

    Represents a comment made by the user on someone else's content.
    """

    message: str | dict | None = None  # Can be string or {"text": "..."}
    actor: str | None = None  # URN of who made the comment
    author: str | None = None  # URN (alternative field name)
    object: str | None = None  # URN of what was commented on
    created: dict | None = None  # {"time": epoch_ms}

    model_config = {"populate_by_name": True}

    def get_text(self) -> str:
        """Extract comment text.

        Handles both string format and dict format {"text": "..."}.

        Returns:
            The comment text, or empty string if not found.
        """
        if isinstance(self.message, dict):
            return self.message.get("text", "")
        return self.message or ""


class ReactionActivity(BaseModel):
    """Activity data for socialActions/likes resource.

    Represents a reaction (like, celebrate, etc.) made by the user.
    """

    reaction_type: str = Field(default="LIKE", alias="reactionType")
    actor: str | None = None  # URN of who reacted
    object: str | None = None  # URN of what was reacted to
    created: dict | None = None  # {"time": epoch_ms}

    model_config = {"populate_by_name": True}


class InvitationActivity(BaseModel):
    """Activity data for invitations resource.

    Represents a connection invitation sent or received.
    """

    message: str | None = None  # Optional invitation message
    invitation_type: str | None = Field(default=None, alias="invitationType")
    inviter: str | None = None  # URN of who sent the invitation
    invitee: str | None = None  # URN of who received the invitation

    model_config = {"populate_by_name": True}


class MessageActivity(BaseModel):
    """Activity data for messages resource.

    Represents a LinkedIn direct message sent or received.
    """

    content: dict = Field(default_factory=dict)  # {"fallback": "text", "format": "TEXT"}
    author: str | None = None  # URN of message sender
    thread: str | None = None  # URN of messaging thread
    created_at: int | None = Field(default=None, alias="createdAt")
    delivered_at: int | None = Field(default=None, alias="deliveredAt")

    model_config = {"populate_by_name": True}

    def get_text(self) -> str:
        """Extract message text from content.

        Returns:
            The message text, or empty string if not found.
        """
        return self.content.get("fallback", "")


class ProfileActivity(BaseModel):
    """Activity data for people resource (profile updates).

    Represents updates to the user's LinkedIn profile.
    """

    headline: dict = Field(default_factory=dict)  # {"localized": {"en_US": "text"}}
    summary: dict = Field(default_factory=dict)
    last_modified: int | None = Field(default=None, alias="lastModified")

    model_config = {"populate_by_name": True}

    def get_headline(self) -> str:
        """Extract headline text.

        Returns:
            The headline text, or empty string if not found.
        """
        localized = self.headline.get("localized", {})
        return localized.get("en_US", "")

    def get_summary(self) -> str:
        """Extract summary text.

        Returns:
            The summary text, or empty string if not found.
        """
        localized = self.summary.get("localized", {})
        return localized.get("en_US", "")


class PositionActivity(BaseModel):
    """Activity data for people/positions resource.

    Represents updates to a job position on the user's profile.
    """

    title: dict = Field(default_factory=dict)  # {"localized": {"en_US": "Job Title"}}
    company_name: dict = Field(default_factory=dict, alias="companyName")

    model_config = {"populate_by_name": True}

    def get_title(self) -> str:
        """Extract position title.

        Returns:
            The position title, or empty string if not found.
        """
        localized = self.title.get("localized", {})
        return localized.get("en_US", "")

    def get_company(self) -> str:
        """Extract company name.

        Returns:
            The company name, or empty string if not found.
        """
        localized = self.company_name.get("localized", {})
        return localized.get("en_US", "")


# Registry of known resource types and their activity schemas.
# Used to validate that we have schemas for all supported resource types.
RESOURCE_SCHEMAS: dict[str, type[BaseModel]] = {
    "ugcPosts": UgcPostActivity,
    "socialActions/comments": CommentActivity,
    "socialActions/likes": ReactionActivity,
    "invitations": InvitationActivity,
    "messages": MessageActivity,
    "people": ProfileActivity,
    "people/positions": PositionActivity,
}

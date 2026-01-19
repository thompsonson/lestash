"""LinkedIn Changelog API response schemas.

These schemas document the expected structure of LinkedIn API responses.
When LinkedIn changes their API, tests will fail, prompting updates.

Reference: https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api
"""

from typing import Literal

from pydantic import BaseModel, Field


class ChangelogEvent(BaseModel):
    """A single changelog event from LinkedIn's Member Changelog API.

    The Changelog API returns activity records for actions taken after
    the user consented to data access. Each event contains metadata about
    the action and optionally the activity data itself.

    Attributes:
        id: Unique identifier for the activity event
        resource_name: Name of resource (e.g., "ugcPosts", "socialActions/comments")
        resource_id: Identifier of the resource
        resource_uri: URI of the resource being modified
        method: Resource method (CREATE, UPDATE, PARTIAL_UPDATE, DELETE, or ACTION)
        processed_at: Time the event was processed (epoch milliseconds)
        captured_at: Time the event was captured (epoch milliseconds)
        activity: Original activity data (empty for DELETE events)
        activity_status: Status of the activity (SUCCESS, FAILURE, SUCCESSFUL_REPLAY)
        owner: Member who owns the record (URN)
        actor: Member who performed the action (URN)
    """

    id: int | None = None
    resource_name: str = Field(alias="resourceName")
    resource_id: str | None = Field(default=None, alias="resourceId")
    resource_uri: str | None = Field(default=None, alias="resourceUri")
    method: Literal["CREATE", "UPDATE", "PARTIAL_UPDATE", "DELETE", "ACTION"]
    processed_at: int | None = Field(default=None, alias="processedAt")  # epoch ms
    captured_at: int | None = Field(default=None, alias="capturedAt")  # epoch ms
    activity: dict | None = None  # Parsed by resource-specific schemas
    activity_status: str | None = Field(default=None, alias="activityStatus")
    owner: str | None = None  # URN
    actor: str | None = None  # URN

    model_config = {"populate_by_name": True}

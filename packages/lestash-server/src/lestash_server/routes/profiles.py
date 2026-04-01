"""Profile API endpoints."""

from fastapi import APIRouter
from lestash.core.database import list_person_profiles, upsert_person_profile
from pydantic import BaseModel

from lestash_server.deps import get_db
from lestash_server.models import ProfileResponse

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileUpdateRequest(BaseModel):
    """Request body for creating/updating a profile."""

    display_name: str
    profile_url: str | None = None


@router.get("", response_model=list[ProfileResponse])
def get_profiles():
    """List all person profiles."""
    with get_db() as conn:
        profiles = list_person_profiles(conn)
    return [ProfileResponse(**p) for p in profiles]


@router.put("/{urn:path}", response_model=ProfileResponse)
def update_profile(urn: str, body: ProfileUpdateRequest):
    """Create or update a person profile mapping."""
    with get_db() as conn:
        upsert_person_profile(
            conn,
            urn=urn,
            display_name=body.display_name,
            profile_url=body.profile_url,
            source="web",
        )
        conn.commit()
    return ProfileResponse(
        urn=urn,
        display_name=body.display_name,
        profile_url=body.profile_url,
        source="web",
    )

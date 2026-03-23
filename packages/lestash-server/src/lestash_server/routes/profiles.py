"""Profile API endpoints."""

from fastapi import APIRouter
from lestash.core.database import list_person_profiles

from lestash_server.deps import get_db
from lestash_server.models import ProfileResponse

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileResponse])
def get_profiles():
    """List all person profiles."""
    with get_db() as conn:
        profiles = list_person_profiles(conn)
    return [ProfileResponse(**p) for p in profiles]

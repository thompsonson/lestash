"""Tag model for Le Stash."""

from pydantic import BaseModel


class Tag(BaseModel):
    """Model for a tag."""

    id: int
    name: str

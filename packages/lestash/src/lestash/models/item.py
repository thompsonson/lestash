"""Item model for Le Stash."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MediaCreate(BaseModel):
    """Model for attaching media to an item."""

    media_type: str  # 'image', 'pdf', 'link', 'thumbnail'
    url: str | None = None
    local_path: str | None = None
    mime_type: str | None = None
    alt_text: str | None = None
    position: int = 0
    source_origin: str = "sync"


class MediaAttachment(BaseModel):
    """Model for a media attachment from the database."""

    id: int
    item_id: int
    media_type: str
    url: str | None = None
    local_path: str | None = None
    mime_type: str | None = None
    alt_text: str | None = None
    position: int = 0
    source_origin: str = "sync"
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> "MediaAttachment":
        """Create a MediaAttachment from a database row."""
        return cls(**dict(row))


class ItemCreate(BaseModel):
    """Model for creating a new item."""

    source_type: str
    source_id: str | None = None
    url: str | None = None
    title: str | None = None
    content: str
    author: str | None = None
    created_at: datetime | None = None
    is_own_content: bool = False
    metadata: dict[str, Any] | None = None
    parent_id: int | None = None
    media: list[MediaCreate] | None = None


class Item(BaseModel):
    """Model for an item in the knowledge base."""

    id: int
    source_type: str
    source_id: str | None = None
    url: str | None = None
    title: str | None = None
    content: str
    author: str | None = None
    created_at: datetime | None = None
    fetched_at: datetime
    is_own_content: bool = False
    metadata: dict[str, Any] | None = None
    parent_id: int | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        """Create an Item from a database row."""
        import json

        data = dict(row)
        if data.get("metadata"):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)

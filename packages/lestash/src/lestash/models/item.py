"""Item model for Le Stash."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


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

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        """Create an Item from a database row."""
        import json

        data = dict(row)
        if data.get("metadata"):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)

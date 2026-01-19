"""Source model for Le Stash."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class Source(BaseModel):
    """Model for a content source configuration."""

    id: int
    source_type: str
    config: dict[str, Any] | None = None
    last_sync: datetime | None = None
    enabled: bool = True

    @classmethod
    def from_row(cls, row: Any) -> "Source":
        """Create a Source from a database row."""
        import json

        data = dict(row)
        if data.get("config"):
            data["config"] = json.loads(data["config"])
        return cls(**data)

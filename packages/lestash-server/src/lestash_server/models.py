"""Pydantic response models for the API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MediaResponse(BaseModel):
    """Media attachment for API responses."""

    id: int
    media_type: str
    url: str | None = None
    serve_url: str
    alt_text: str | None = None
    position: int = 0


class ItemResponse(BaseModel):
    """Enriched item for API responses."""

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
    # Enriched fields
    subtype: str
    author_display: str
    actor_display: str
    preview: str
    tags: list[str] = []
    child_count: int = 0
    media: list[MediaResponse] = []


class ItemListResponse(BaseModel):
    """Paginated item list."""

    items: list[ItemResponse]
    total: int
    limit: int
    offset: int


class SourceResponse(BaseModel):
    """Source plugin status."""

    name: str
    description: str
    enabled: bool
    last_sync: str | None = None


class SyncLogEntry(BaseModel):
    """A single sync log entry."""

    source_type: str
    started_at: str | None = None
    completed_at: str | None = None
    status: str
    items_added: int = 0
    items_updated: int = 0
    error_message: str | None = None


class ProfileResponse(BaseModel):
    """Person profile."""

    urn: str
    profile_url: str | None = None
    display_name: str | None = None
    source: str | None = None


class StatsResponse(BaseModel):
    """Knowledge base statistics."""

    total_items: int
    sources: dict[str, int]
    own_content: int
    date_range: dict[str, str | None]
    last_syncs: dict[str, str | None]


class ItemCreateRequest(BaseModel):
    """Request body for creating a single item."""

    source_type: str
    source_id: str | None = None
    url: str | None = None
    title: str | None = None
    content: str
    author: str | None = None
    created_at: datetime | None = None
    is_own_content: bool = True
    metadata: dict[str, Any] | None = None
    parent_id: int | None = None


class CollectionCreate(BaseModel):
    """Request to create a collection."""

    name: str
    description: str | None = None


class CollectionResponse(BaseModel):
    """Collection summary."""

    id: int
    name: str
    description: str | None = None
    item_count: int = 0
    created_at: datetime | None = None


class CollectionDetailResponse(BaseModel):
    """Collection with its items."""

    id: int
    name: str
    description: str | None = None
    items: list[ItemResponse]
    created_at: datetime | None = None


class CollectionItemAdd(BaseModel):
    """Request to add an item to a collection."""

    item_id: int
    note: str | None = None


class ImportResponse(BaseModel):
    """Response from file import."""

    status: str
    source_type: str
    items_added: int
    items_updated: int
    errors: list[str]


class DriveImportRequest(BaseModel):
    """Request to import files from Google Drive."""

    file_ids: list[str]


class RefineRequest(BaseModel):
    """Request to refine a transcript via LLM."""

    text: str
    prompt: str | None = None
    model: str | None = None


class RefineResponse(BaseModel):
    """Response from LLM refinement."""

    refined_text: str
    model_used: str
    prompt_used: str


class TranscribeRequest(BaseModel):
    """Request options for audio transcription."""

    model: str = "base.en"
    title: str | None = None


class TranscribeResponse(BaseModel):
    """Response from audio transcription."""

    text: str
    language: str
    duration_seconds: float
    model: str
    item_id: int
    title: str


class TagInfo(BaseModel):
    """A tag with its usage count."""

    name: str
    count: int


class TagListResponse(BaseModel):
    """List of all tags."""

    tags: list[TagInfo]


class TagAddRequest(BaseModel):
    """Request to add a tag to an item."""

    name: str


class HealthResponse(BaseModel):
    """Server health check."""

    status: str = "ok"
    version: str
    items: int

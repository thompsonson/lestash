"""Pydantic response models for the API."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
    # Enriched fields
    subtype: str
    author_display: str
    actor_display: str
    preview: str


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


class ImportResponse(BaseModel):
    """Response from file import."""

    status: str
    source_type: str
    items_added: int
    items_updated: int
    errors: list[str]


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


class HealthResponse(BaseModel):
    """Server health check."""

    status: str = "ok"
    version: str
    items: int

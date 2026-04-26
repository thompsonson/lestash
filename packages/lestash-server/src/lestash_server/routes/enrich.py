"""PDF enrichment API endpoints.

POST /api/items/{item_id}/enrich      — enrich a single item (idempotent)
POST /api/enrich/all                  — enrich every PDF item
POST /api/enrich/backfill-sources     — Flow 1.5 source-PDF backfill
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from lestash.core.database import get_connection
from lestash.core.pdf_enrich import (
    backfill_source_pdfs,
    enrich_item,
    list_pdf_items,
)
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enrich"])


class EnrichRequest(BaseModel):
    force: bool = False


class EnrichResponse(BaseModel):
    item_id: int
    status: str
    images: int = 0
    annotations: int = 0
    message: str | None = None


class EnrichAllResponse(BaseModel):
    total: int
    enriched: int
    skipped: int
    source_unavailable: int
    failed: int


class BackfillResponse(BaseModel):
    inspected: int
    backfilled: int
    already_present: int
    unavailable: int


@router.post("/api/items/{item_id}/enrich", response_model=EnrichResponse)
def enrich_one(item_id: int, body: EnrichRequest | None = None) -> EnrichResponse:
    force = bool(body and body.force)
    result = enrich_item(item_id, force=force)
    if result.status == "failed" and result.message == "item not found":
        raise HTTPException(status_code=404, detail="item not found")
    return EnrichResponse(
        item_id=result.item_id,
        status=result.status,
        images=result.images,
        annotations=result.annotations,
        message=result.message,
    )


@router.post("/api/enrich/all", response_model=EnrichAllResponse)
def enrich_all(body: EnrichRequest | None = None) -> EnrichAllResponse:
    force = bool(body and body.force)
    with get_connection() as conn:
        ids = list_pdf_items(conn)

    counts = {"enriched": 0, "skipped": 0, "source_unavailable": 0, "failed": 0}
    for item_id in ids:
        result = enrich_item(item_id, force=force)
        counts[result.status] = counts.get(result.status, 0) + 1
    return EnrichAllResponse(
        total=len(ids),
        enriched=counts["enriched"],
        skipped=counts["skipped"],
        source_unavailable=counts["source_unavailable"],
        failed=counts["failed"],
    )


@router.post("/api/enrich/backfill-sources", response_model=BackfillResponse)
def backfill() -> BackfillResponse:
    stats = backfill_source_pdfs()
    return BackfillResponse(
        inspected=stats.inspected,
        backfilled=stats.backfilled,
        already_present=stats.already_present,
        unavailable=stats.unavailable,
    )


class OcrRequest(BaseModel):
    item_id: int | None = None


class OcrResponse(BaseModel):
    total: int
    ocrd: int
    skipped: int
    unavailable: int
    failed: int


@router.post("/api/enrich/ocr", response_model=OcrResponse)
def run_ocr(body: OcrRequest | None = None) -> OcrResponse:
    """Transcribe handwritten ink annotations via Claude vision. Requires
    ANTHROPIC_API_KEY. Idempotent at the (stroke_geometry_hash, ocr_version)
    granularity."""
    from lestash.core.pdf_enrich import ocr_pending_annotations

    item_id = body.item_id if body else None
    results = ocr_pending_annotations(item_id=item_id)
    counts: dict[str, int] = {"transcribed": 0, "skipped": 0, "unavailable": 0, "failed": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return OcrResponse(
        total=len(results),
        ocrd=counts["transcribed"],
        skipped=counts["skipped"],
        unavailable=counts["unavailable"],
        failed=counts["failed"],
    )

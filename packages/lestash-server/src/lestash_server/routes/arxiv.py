"""arXiv API endpoints."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from lestash_server.deps import get_config, get_db

router = APIRouter(prefix="/api/arxiv", tags=["arxiv"])
logger = logging.getLogger(__name__)


class ImportPaperRequest(BaseModel):
    """Request to import an arXiv paper as its arXiv item."""

    url: str
    note: str | None = None
    # A new paper goes under this item (+ Add → New link); one already held stays put
    parent_id: int | None = None


class ImportPaperResponse(BaseModel):
    """Response after importing a paper."""

    item_id: int
    title: str
    created: bool  # True if a new item was made, False if the paper was already held
    fetching_pdf: bool  # the PDF is being downloaded and read after this answer


def _attach_pdf(item_id: int) -> None:
    from lestash_arxiv.share import attach_pdf

    try:
        status = attach_pdf(item_id, config=get_config())
        logger.info("arXiv item %d: PDF %s", item_id, status)
    except Exception:
        logger.exception("Could not store the PDF of arXiv item %d", item_id)


@router.post("/import-paper", response_model=ImportPaperResponse)
def import_paper(body: ImportPaperRequest, background: BackgroundTasks):
    """Save a shared arXiv paper as its arXiv item, then store and read its PDF (#395).

    A paper already held is reused, not duplicated; it gets its PDF if it lacks one.
    The PDF is downloaded and enriched after the answer, as that takes a while.
    """
    try:
        from lestash_arxiv.share import arxiv_id_from_url
        from lestash_arxiv.share import import_paper as save_paper
    except ImportError as e:
        raise HTTPException(status_code=501, detail="lestash-arxiv not installed") from e

    arxiv_id = arxiv_id_from_url(body.url)
    if not arxiv_id:
        raise HTTPException(status_code=400, detail="Not an arXiv paper address")

    try:
        with get_db() as conn:
            imported = save_paper(conn, arxiv_id, note=body.note, parent_id=body.parent_id)
    except Exception as e:
        logger.exception("Failed to import arXiv paper %s", arxiv_id)
        raise HTTPException(status_code=502, detail="arXiv could not be reached") from e
    if imported is None:
        raise HTTPException(status_code=404, detail="Paper not found on arXiv")

    if not imported.has_pdf:
        background.add_task(_attach_pdf, imported.item_id)
    return ImportPaperResponse(
        item_id=imported.item_id,
        title=imported.title,
        created=imported.created,
        fetching_pdf=not imported.has_pdf,
    )

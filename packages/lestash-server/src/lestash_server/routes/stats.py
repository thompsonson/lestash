"""Stats API endpoint."""

from fastapi import APIRouter

from lestash_server.deps import get_db
from lestash_server.models import StatsResponse

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats", response_model=StatsResponse)
def get_stats():
    """Get knowledge base statistics."""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

        own = conn.execute("SELECT COUNT(*) FROM items WHERE is_own_content = 1").fetchone()[0]

        # Counts by source
        rows = conn.execute(
            "SELECT source_type, COUNT(*) as cnt FROM items GROUP BY source_type ORDER BY cnt DESC"
        ).fetchall()
        sources = {row["source_type"]: row["cnt"] for row in rows}

        # Date range
        date_row = conn.execute(
            "SELECT MIN(created_at) as earliest, MAX(created_at) as latest FROM items"
        ).fetchone()

        # Last syncs per source
        sync_rows = conn.execute(
            "SELECT source_type, last_sync FROM sources WHERE last_sync IS NOT NULL"
        ).fetchall()
        last_syncs = {row["source_type"]: row["last_sync"] for row in sync_rows}

    return StatsResponse(
        total_items=total,
        sources=sources,
        own_content=own,
        date_range={
            "earliest": date_row["earliest"],
            "latest": date_row["latest"],
        },
        last_syncs=last_syncs,
    )

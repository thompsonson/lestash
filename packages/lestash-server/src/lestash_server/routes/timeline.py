"""The Timeline: what you did and what arrived, between two times (#262).

Entries are activities (watched, liked, prompted...) and items you made in that time
(posts, notes, notebooks, chats...), newest first, each with a short summary of its
item. The app folds busy verbs into one line; the server returns every entry.
"""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from lestash.core.kinds import ARTIFACT_LABELS, KINDS
from pydantic import BaseModel

from lestash_server.deps import get_db

router = APIRouter(prefix="/api/timeline", tags=["timeline"])

# Items that show on the day they were made: things you wrote or built. A paper's or
# a video's date is when it was published, not when it reached you, so they show by
# their activities instead
MADE_KINDS = ("post", "comment", "note", "notebook", "chat", "artifact", "project", "email")
MAX_ENTRIES = 5000


class ItemSummary(BaseModel):
    id: int
    title: str
    kind: str
    kind_label: str
    kind_detail: str | None = None
    source_type: str
    parent_id: int | None = None
    parent_title: str | None = None
    channel: str | None = None  # a video's channel


class TimelineEntry(BaseModel):
    at: str  # UTC, ISO 8601
    verb: str  # an activity's verb, or "made"
    item: ItemSummary | None = None
    context: ItemSummary | None = None  # e.g. the notebook a prompt used
    details: dict | None = None


class TimelineResponse(BaseModel):
    start: str
    end: str
    entries: list[TimelineEntry]
    truncated: bool = False
    # The nearest times outside the span that have entries, for day steps
    previous: str | None = None
    next: str | None = None


def _utc(value: str, name: str) -> str:
    """An ISO time as UTC 'YYYY-MM-DDTHH:MM:SS', the form activities are stored in."""
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"{name}: not an ISO time") from e
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


_SUMMARY = """
    i.id, coalesce(nullif(i.title, ''), substr(i.content, 1, 120)) AS title, i.kind,
    i.source_type, i.parent_id, p.title AS parent_title,
    CASE WHEN json_valid(i.metadata) THEN json_extract(i.metadata, '$.channel_title') END
        AS channel,
    CASE WHEN json_valid(i.metadata) THEN json_extract(i.metadata, '$.artifact_type') END
        AS artifact_type
"""


def _summary(row, prefix: str = "") -> ItemSummary | None:
    if row[f"{prefix}id"] is None:
        return None
    kind = row[f"{prefix}kind"] or "document"
    artifact = row[f"{prefix}artifact_type"]
    return ItemSummary(
        id=row[f"{prefix}id"],
        title=(row[f"{prefix}title"] or "").strip() or "(untitled)",
        kind=kind,
        kind_label=KINDS[kind].label if kind in KINDS else kind,
        kind_detail=ARTIFACT_LABELS.get(artifact, artifact) if kind == "artifact" else None,
        source_type=row[f"{prefix}source_type"],
        parent_id=row[f"{prefix}parent_id"],
        parent_title=row[f"{prefix}parent_title"],
        channel=row[f"{prefix}channel"] if isinstance(row[f"{prefix}channel"], str) else None,
    )


def _made_where(kinds) -> str:
    """Items you made, of these kinds: yours, or built for you (a notebook, a chat)."""
    return (
        f"i.held = 1 AND i.kind IN ({', '.join('?' for _ in kinds)})"
        " AND (i.is_own_content = 1 OR i.kind IN ('notebook', 'artifact', 'project', 'chat'))"
    )


def _filter(values: str | None) -> list[str]:
    return sorted({v.strip() for v in (values or "").split(",") if v.strip()})


@router.get("", response_model=TimelineResponse)
def timeline(
    start: str = Query(..., description="From this time (ISO 8601; a day's start in your zone)"),
    end: str = Query(..., description="Up to this time, not included"),
    verb: str | None = Query(None, description="Comma-separated verbs to keep; 'made' for items"),
    kind: str | None = Query(None, description="Comma-separated item kinds to keep"),
):
    """Activities and made items between start and end, newest first."""
    lo, hi = _utc(start, "start"), _utc(end, "end")
    verbs, kinds = _filter(verb), _filter(kind)
    want_made = not verbs or "made" in verbs
    act_verbs = [v for v in verbs if v != "made"]

    act_where = ["a.occurred_at >= ?", "a.occurred_at < ?"]
    act_params: list = [lo, hi]
    if act_verbs:
        act_where.append(f"a.verb IN ({', '.join('?' for _ in act_verbs)})")
        act_params += act_verbs
    if kinds:
        act_where.append(f"i.kind IN ({', '.join('?' for _ in kinds)})")
        act_params += kinds

    made_kinds = [k for k in MADE_KINDS if not kinds or k in kinds]

    with get_db() as conn:
        entries: list[TimelineEntry] = []
        if not verbs or act_verbs:
            rows = conn.execute(
                f"""SELECT a.occurred_at, a.verb, a.details, {_SUMMARY},
                       c.id AS c_id, c.title AS c_title, c.kind AS c_kind,
                       c.source_type AS c_source_type, c.parent_id AS c_parent_id,
                       NULL AS c_parent_title, NULL AS c_channel, NULL AS c_artifact_type
                FROM activities a
                LEFT JOIN items i ON i.id = a.item_id
                LEFT JOIN items p ON p.id = i.parent_id
                LEFT JOIN items c ON c.id = a.context_id
                WHERE {" AND ".join(act_where)} AND a.actor_id IS NULL
                ORDER BY a.occurred_at DESC LIMIT ?""",
                [*act_params, MAX_ENTRIES + 1],
            ).fetchall()
            for r in rows:
                details = None
                if r["details"]:
                    try:
                        details = json.loads(r["details"])
                    except ValueError:
                        details = None
                entries.append(
                    TimelineEntry(
                        at=r["occurred_at"],
                        verb=r["verb"],
                        item=_summary(r),
                        context=_summary(r, "c_"),
                        details=details if isinstance(details, dict) else None,
                    )
                )
        if want_made and made_kinds:
            made_where = _made_where(made_kinds)
            rows = conn.execute(
                f"""SELECT strftime('%Y-%m-%dT%H:%M:%SZ', i.created_at) AS at, {_SUMMARY}
                FROM items i LEFT JOIN items p ON p.id = i.parent_id
                WHERE {made_where}
                  AND datetime(i.created_at) >= datetime(?) AND datetime(i.created_at) < datetime(?)
                ORDER BY datetime(i.created_at) DESC LIMIT ?""",
                [*made_kinds, lo, hi, MAX_ENTRIES + 1],
            ).fetchall()
            entries += [TimelineEntry(at=r["at"], verb="made", item=_summary(r)) for r in rows]
        entries.sort(key=lambda e: e.at, reverse=True)
        truncated = len(entries) > MAX_ENTRIES

        def nearest(op: str, order: str, bound: str) -> str | None:
            act = conn.execute(
                f"SELECT occurred_at FROM activities WHERE occurred_at {op} ? AND actor_id IS NULL"
                f" ORDER BY occurred_at {order} LIMIT 1",
                (bound,),
            ).fetchone()
            made = conn.execute(
                f"""SELECT strftime('%Y-%m-%dT%H:%M:%SZ', i.created_at) FROM items i
                WHERE {_made_where(MADE_KINDS)} AND datetime(i.created_at) {op} datetime(?)
                ORDER BY datetime(i.created_at) {order} LIMIT 1""",
                [*MADE_KINDS, bound],
            ).fetchone()
            found = [x[0] for x in (act, made) if x and x[0]]
            if not found:
                return None
            return max(found) if order == "DESC" else min(found)

        previous = nearest("<", "DESC", lo)
        following = nearest(">=", "ASC", hi)

    return TimelineResponse(
        start=lo,
        end=hi,
        entries=entries[:MAX_ENTRIES],
        truncated=truncated,
        previous=previous,
        next=following,
    )

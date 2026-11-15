"""The Timeline: what you did and what arrived, between two times (#262).

Entries are activities (watched, liked, prompted...) and items you made in that time
(posts, notes, notebooks, chats...), newest first, each with a short summary of its
item. The app folds busy verbs into one line; the server returns every entry.
"""

import html
import json
import re
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
PROMPT_CHARS = 280

# A prompt is the chat's turn by you at the activity's time (within a second: the
# two are stored in different forms)
_PROMPT = """
    CASE WHEN a.verb = 'prompted' THEN (
        SELECT substr(t.content, 1, ?) FROM items t
        WHERE t.parent_id = a.item_id
          AND abs(julianday(t.created_at) - julianday(a.occurred_at)) < 1.0 / 86400
          AND json_valid(t.metadata)
          AND coalesce(json_extract(t.metadata, '$.role'), json_extract(t.metadata, '$.sender'))
              IN ('user', 'human')
        ORDER BY t.id LIMIT 1
    ) END AS prompt
"""


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
    # A stub (not held): what it stands for, where it is, and when it was made. Its
    # title is the cached preview of the post, if any; its author the post's (#320)
    held: bool = True
    stub_of: str | None = None  # 'post' or 'comment'
    url: str | None = None
    made_at: str | None = None
    author: str | None = None


class TimelineEntry(BaseModel):
    at: str  # UTC, ISO 8601
    verb: str  # an activity's verb, or "made"
    item: ItemSummary | None = None
    context: ItemSummary | None = None  # e.g. the notebook a prompt used
    details: dict | None = None
    prompt: str | None = None  # for "prompted": the start of what you asked


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
        AS artifact_type,
    i.held, i.url, strftime('%Y-%m-%dT%H:%M:%SZ', i.created_at) AS made_at,
    CASE WHEN i.held = 0 AND json_valid(i.metadata) THEN
        CASE WHEN json_extract(i.metadata, '$.comment_urn') IS NOT NULL THEN 'comment'
             WHEN json_extract(i.metadata, '$.post_urn') IS NOT NULL THEN 'post' END
    END AS stub_of,
    pc.content_preview AS preview, pc.author_name AS author
"""

# A LinkedIn stub's post, as its public preview was cached (lestash_linkedin.feed_preview)
_PREVIEW = """
    LEFT JOIN post_cache pc ON i.held = 0 AND json_valid(i.metadata)
        AND pc.urn = json_extract(i.metadata, '$.post_urn')
"""
PREVIEW_CHARS = 120
# LinkedIn's preview can end with its comment count: "Context is everywhere! | 13 comments…"
_COMMENT_COUNT = re.compile(r"\s*\|\s*[\d,]+ comments? on LinkedIn\s*$")


def preview_title(text: str | None, stub_of: str | None) -> str | None:
    """A stub's title from its post's cached preview: the first line, cut short."""
    # The preview is the page's og:description as written: HTML entities and all
    text = html.unescape(text or "")
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = _COMMENT_COUNT.sub("", line)
    if not line:
        return None
    if len(line) > PREVIEW_CHARS:
        line = line[: PREVIEW_CHARS - 1].rstrip() + "…"
    return f"A reply on: {line}" if stub_of == "comment" else line


def _summary(row, prefix: str = "") -> ItemSummary | None:
    if row[f"{prefix}id"] is None:
        return None
    kind = row[f"{prefix}kind"] or "document"
    artifact = row[f"{prefix}artifact_type"]
    has = set(row.keys())

    def col(name: str):
        return row[f"{prefix}{name}"] if f"{prefix}{name}" in has else None

    held = col("held") != 0
    stub_of = None if held else col("stub_of")
    title = (row[f"{prefix}title"] or "").strip()
    if not held and not title:
        # A stub has no title of its own: its post's preview, else the app names it
        title = preview_title(col("preview"), stub_of) or ""
    return ItemSummary(
        id=row[f"{prefix}id"],
        title=title or ("" if not held else "(untitled)"),
        kind=kind,
        kind_label=KINDS[kind].label if kind in KINDS else kind,
        kind_detail=ARTIFACT_LABELS.get(artifact, artifact) if kind == "artifact" else None,
        source_type=row[f"{prefix}source_type"],
        parent_id=row[f"{prefix}parent_id"],
        parent_title=row[f"{prefix}parent_title"],
        channel=row[f"{prefix}channel"] if isinstance(row[f"{prefix}channel"], str) else None,
        held=held,
        stub_of=stub_of,
        url=col("url"),
        made_at=None if held else col("made_at"),
        author=None if held or not col("author") else html.unescape(col("author")),
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
                       NULL AS c_parent_title, NULL AS c_channel, NULL AS c_artifact_type,
                       {_PROMPT}
                FROM activities a
                LEFT JOIN items i ON i.id = a.item_id
                LEFT JOIN items p ON p.id = i.parent_id
                {_PREVIEW}
                LEFT JOIN items c ON c.id = a.context_id
                WHERE {" AND ".join(act_where)} AND a.actor_id IS NULL
                ORDER BY a.occurred_at DESC LIMIT ?""",
                [PROMPT_CHARS, *act_params, MAX_ENTRIES + 1],
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
                        prompt=(r["prompt"] or "").strip() or None,
                    )
                )
        if want_made and made_kinds:
            made_where = _made_where(made_kinds)
            rows = conn.execute(
                f"""SELECT strftime('%Y-%m-%dT%H:%M:%SZ', i.created_at) AS at, {_SUMMARY}
                FROM items i LEFT JOIN items p ON p.id = i.parent_id
                {_PREVIEW}
                WHERE {made_where}
                  AND datetime(i.created_at) >= datetime(?) AND datetime(i.created_at) < datetime(?)
                ORDER BY datetime(i.created_at) DESC LIMIT ?""",
                [*made_kinds, lo, hi, MAX_ENTRIES + 1],
            ).fetchall()
            entries += [TimelineEntry(at=r["at"], verb="made", item=_summary(r)) for r in rows]
        entries.sort(key=lambda e: e.at, reverse=True)
        truncated = len(entries) > MAX_ENTRIES

        # The nearest entries outside the span, under the same filters, so a step
        # never lands on a day the filters leave empty
        def nearest(op: str, order: str, bound: str) -> str | None:
            found = []
            if not verbs or act_verbs:
                filters = act_where[2:]  # the verb and kind conditions, without the span
                act = conn.execute(
                    f"""SELECT a.occurred_at FROM activities a
                    LEFT JOIN items i ON i.id = a.item_id
                    WHERE a.occurred_at {op} ? AND a.actor_id IS NULL
                    {"".join(" AND " + f for f in filters)}
                    ORDER BY a.occurred_at {order} LIMIT 1""",
                    [bound, *act_params[2:]],
                ).fetchone()
                if act:
                    found.append(act[0])
            if want_made and made_kinds:
                made = conn.execute(
                    f"""SELECT strftime('%Y-%m-%dT%H:%M:%SZ', i.created_at) FROM items i
                    WHERE {_made_where(made_kinds)} AND datetime(i.created_at) {op} datetime(?)
                    ORDER BY datetime(i.created_at) {order} LIMIT 1""",
                    [*made_kinds, bound],
                ).fetchone()
                if made and made[0]:
                    found.append(made[0])
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

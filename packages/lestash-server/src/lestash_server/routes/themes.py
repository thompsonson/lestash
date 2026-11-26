"""Suggested themes: groups of related items across sources, to keep as a
collection or dismiss (#266)."""

import json

from fastapi import APIRouter, HTTPException
from lestash.core.kinds import KINDS
from lestash.core.themes import ThemeGone, dismiss, keep, suggested
from pydantic import BaseModel

from lestash_server.deps import get_db

router = APIRouter(prefix="/api/themes", tags=["themes"])

EXCERPT_CHARS = 90  # an untitled item is shown by its text's start


def _excerpt(content: str | None) -> str | None:
    text = " ".join((content or "").split())
    if not text:
        return None
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


class ThemeItem(BaseModel):
    id: int
    source_type: str
    kind: str | None = None
    kind_label: str | None = None
    kind_plural: str | None = None  # "Replies": for counting items by kind
    title: str | None = None
    excerpt: str | None = None  # the text's start, for an item with no title


class ThemeResponse(BaseModel):
    """A suggested theme and its items, in its order."""

    id: int
    name: str
    sources: list[str]  # most items first
    items: list[ThemeItem]


class ThemeKeep(BaseModel):
    name: str | None = None  # the collection's name; the theme's by default


class ThemeKept(BaseModel):
    collection_id: int


@router.get("", response_model=list[ThemeResponse])
def list_themes():
    """The suggested themes, largest first. Items deleted since are left out."""
    with get_db() as conn:
        themes = []
        for row in suggested(conn):
            ids = json.loads(row["item_ids"])
            found = {
                r["id"]: r
                for r in conn.execute(
                    "SELECT id, source_type, kind, title, substr(content, 1, 400) AS content "
                    "FROM items WHERE id IN "
                    f"({','.join('?' * len(ids))})",
                    ids,
                )
            }
            items = [
                ThemeItem(
                    id=i,
                    source_type=found[i]["source_type"],
                    kind=found[i]["kind"],
                    kind_label=KINDS[k].label if (k := found[i]["kind"]) in KINDS else k,
                    kind_plural=KINDS[k].plural if k in KINDS else k,
                    title=found[i]["title"],
                    excerpt=None
                    if (found[i]["title"] or "").strip()
                    else _excerpt(found[i]["content"]),
                )
                for i in ids
                if i in found
            ]
            counts: dict[str, int] = {}
            for item in items:
                counts[item.source_type] = counts.get(item.source_type, 0) + 1
            sources = sorted(counts, key=lambda s: (-counts[s], s))
            themes.append(
                ThemeResponse(id=row["id"], name=row["name"], sources=sources, items=items)
            )
        return themes


@router.post("/{theme_id}/keep", response_model=ThemeKept, status_code=201)
def keep_theme(theme_id: int, body: ThemeKeep | None = None):
    """Make a collection of the theme's items. ``404`` if it isn't suggested now."""
    with get_db() as conn:
        try:
            collection_id = keep(conn, theme_id, body.name if body else None)
        except ThemeGone as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        conn.commit()
    return ThemeKept(collection_id=collection_id)


@router.post("/{theme_id}/dismiss")
def dismiss_theme(theme_id: int) -> dict[str, str]:
    """Never suggest this theme, or one mostly like it, again."""
    with get_db() as conn:
        try:
            dismiss(conn, theme_id)
        except ThemeGone as e:
            raise HTTPException(status_code=404, detail=str(e)) from None
        conn.commit()
    return {"status": "dismissed"}

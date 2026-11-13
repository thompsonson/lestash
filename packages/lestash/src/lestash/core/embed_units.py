"""What gets a vector, and the text each one is made from (#221, step 2).

The ontology's own parts, not fixed-size chunks (`docs/embeddings-design.md`):

- a top-level item: its title, then the start of its content;
- a chat: each round, your prompt and the replies to it. Without the chat's title:
  in the experiments it made every round of a chat alike (#305). Rounds are derived
  from the turns, not stored; the vector sits on the round's first turn;
- a comment: the start of the post it is on, then the comment;
- a highlight: the book's title, then the highlight;
- any other child (a NotebookLM artifact, a transcript, an email message): like a
  top-level item, for now; sections split the long ones later;
- nothing for a reaction, or a YouTube watch under the same video's like;
- nothing for a video whose text is only its title (watch history, about 61k):
  keyword search finds those, and their vectors crowded semantic search (#305).

Each unit names its recipe, so a vector records which rule made it. Change a rule's
text and give it a new name: the next refresh re-embeds what it touches.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

CONTENT_CHARS = 500
CONTEXT_CHARS = 200
PROMPT_CHARS = 400
REPLY_CHARS = 600

ITEM_RECIPE = "title+content500"
COMMENT_RECIPE = "comment:post200+content500"
HIGHLIGHT_RECIPE = "highlight:book+content500"
ROUND_PREFIX = "round:"  # every round recipe starts so
ROUND_RECIPE = f"{ROUND_PREFIX}prompt400+replies600"
# A video with no more text than its title and this many characters gets no vector
TITLE_ONLY_SLACK = 60
RECIPES = (ITEM_RECIPE, COMMENT_RECIPE, HIGHLIGHT_RECIPE, ROUND_RECIPE)

# A watch-history video: no text but its title
TITLE_ONLY = (
    "({t}.kind IS 'video' AND length(coalesce({t}.content, ''))"
    " <= length(coalesce({t}.title, '')) + ?)"
)

# Children with no text of their own worth a vector
_SKIP_KINDS = {"reaction"}


@dataclass(frozen=True)
class Unit:
    """One vector to make: the item it sits on, the recipe and its text."""

    item_id: int
    recipe: str
    text: str


def embed_source(title: str | None, content: str | None) -> str:
    """The text embedded for an item: its title, then the start of its content."""
    parts = [part for part in (title, (content or "")[:CONTENT_CHARS]) if part]
    return " ".join(parts).strip()


def _join(*parts: str | None) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip())


@dataclass
class _Turn:
    item_id: int
    source_id: str | None
    text: str
    is_prompt: bool
    prompt_source_id: str | None


def rounds(turns: list[_Turn]) -> list[list[_Turn]]:
    """A chat's rounds: each prompt of yours, then the replies to it.

    A reply goes with the prompt it names (Gemini's prompt_source_id), wherever that
    prompt sorts; else with the prompt before it. Replies before any prompt make a
    round of their own.
    """
    # First the prompts, so a reply can name one that sorts after it
    round_of: dict[int, list[_Turn]] = {}
    by_source: dict[str, list[_Turn]] = {}
    for turn in turns:
        if turn.is_prompt:
            round_of[turn.item_id] = [turn]
            if turn.source_id:
                by_source.setdefault(turn.source_id, round_of[turn.item_id])
    leading: list[_Turn] = []
    found: list[list[_Turn]] = [leading]
    current = leading
    for turn in turns:
        if turn.is_prompt:
            current = round_of[turn.item_id]
            found.append(current)
        else:
            by_source.get(turn.prompt_source_id or "", current).append(turn)
    return [r for r in found if r]


def _round_unit(turns: list[_Turn]) -> Unit | None:
    prompt = turns[0].text[:PROMPT_CHARS] if turns[0].is_prompt else ""
    replies = " ".join(t.text for t in turns if not t.is_prompt)[:REPLY_CHARS]
    text = _join(prompt, replies)
    if not (prompt.strip() or replies.strip()):
        return None
    return Unit(turns[0].item_id, ROUND_RECIPE, text)


def _scope(column: str, families: Iterable[int] | None) -> tuple[str, list[int]]:
    if families is None:
        return "", []
    ids = list(families)
    return f" AND {column} IN ({','.join('?' * len(ids))})", ids


def embedding_units(conn: sqlite3.Connection, families: Iterable[int] | None = None) -> list[Unit]:
    """Every vector that should exist, or only those of these top-level items and
    their children."""
    fams = None if families is None else list(families)
    units: list[Unit] = []

    where, args = _scope("id", fams)
    for item_id, title, content in conn.execute(
        "SELECT id, title, substr(content, 1, ?) FROM items "
        f"WHERE parent_id IS NULL AND held = 1 AND NOT {TITLE_ONLY.format(t='items')}{where}",
        [CONTENT_CHARS, TITLE_ONLY_SLACK, *args],
    ):
        text = embed_source(title, content)
        if text:
            units.append(Unit(item_id, ITEM_RECIPE, text))

    where, args = _scope("c.parent_id", fams)
    chats: dict[int, list[_Turn]] = {}
    # Rounds need the longest piece of a turn; the other recipes cut shorter
    longest = max(CONTENT_CHARS, PROMPT_CHARS, REPLY_CHARS)
    for row in conn.execute(
        "SELECT c.id, c.parent_id, c.kind, c.title, substr(c.content, 1, ?), c.source_id,"
        " CASE WHEN json_valid(c.metadata) THEN"
        "  coalesce(json_extract(c.metadata, '$.sender'), json_extract(c.metadata, '$.role'))"
        " END,"
        " CASE WHEN json_valid(c.metadata) THEN"
        "  json_extract(c.metadata, '$.prompt_source_id') END,"
        " p.kind, p.title, substr(p.content, 1, ?)"
        " FROM items c JOIN items p ON p.id = c.parent_id"
        f" WHERE c.held = 1 AND NOT {TITLE_ONLY.format(t='c')}{where}"
        " ORDER BY c.parent_id, datetime(c.created_at), c.id",
        [longest, CONTEXT_CHARS, TITLE_ONLY_SLACK, *args],
    ):
        (item_id, parent_id, kind, title, content, source_id, role, prompt_of) = row[:8]
        parent_kind, parent_title, parent_start = row[8:]
        content = content or ""
        if kind in _SKIP_KINDS or (kind == "video" and parent_kind == "video"):
            continue
        if kind == "turn":
            chats.setdefault(parent_id, []).append(
                _Turn(item_id, source_id, content, role in ("human", "user"), prompt_of)
            )
        elif kind == "comment":
            if content.strip():
                context = parent_title or parent_start
                text = _join(context, content[:CONTENT_CHARS])
                units.append(Unit(item_id, COMMENT_RECIPE, text))
        elif kind == "highlight":
            if content.strip():
                text = _join(parent_title, content[:CONTENT_CHARS])
                units.append(Unit(item_id, HIGHLIGHT_RECIPE, text))
        else:
            text = embed_source(title, content)
            if text:
                units.append(Unit(item_id, ITEM_RECIPE, text))

    for turns in chats.values():
        for found in rounds(turns):
            unit = _round_unit(found)
            if unit:
                units.append(unit)
    return units

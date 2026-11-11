"""Item kinds: what an item is to you (a post, a book, a video), derived from its source.

Items have no kind field of their own; it follows from the source, a few metadata keys,
whether the item is part of another and whether LeStash holds it. ``items.kind`` is a
generated column with the SQL below (migration 24), so listings filter and count by kind
with an index; ``item_kind`` is the same rule in Python, kept equal by tests.

See docs/ux-object-model-design.md, "Item kinds".
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Kind:
    label: str
    plural: str


KINDS: Mapping[str, Kind] = {
    "post": Kind("Post", "Posts"),
    "comment": Kind("Reply", "Replies"),
    "reaction": Kind("Reaction", "Reactions"),
    "message": Kind("Message", "Messages"),
    "video": Kind("Video", "Videos"),
    "search": Kind("Search", "Searches"),
    "transcript": Kind("Transcript", "Transcripts"),
    "paper": Kind("Paper", "Papers"),
    "book": Kind("Book", "Books"),
    "highlight": Kind("Highlight", "Highlights"),
    "chat": Kind("Chat", "Chats"),
    "turn": Kind("Turn", "Turns"),
    "notebook": Kind("Notebook", "Notebooks"),
    "artifact": Kind("Generated", "Generated"),
    "project": Kind("Project", "Projects"),
    "document": Kind("Document", "Documents"),
    "note": Kind("Note", "Notes"),
    "email": Kind("Email thread", "Email threads"),
    "image": Kind("Image", "Images"),
    "stub": Kind("Not held", "Not held"),
    "other": Kind("Other", "Other"),
}

# NotebookLM artifact types, in words
ARTIFACT_LABELS: Mapping[str, str] = {
    "audio_overview": "Audio overview",
    "explainer_video": "Explainer video",
    "slides": "Slides",
    "infographic": "Infographic",
    "app": "App",
    "file": "File",
    "report": "Report",
}

_LINKEDIN_POSTS = ("ugcPosts", "instantReposts", "article", "originalArticles")
_BOOK_PARTS = ("highlight", "markup", "dogear", "note")


def _text(metadata: Mapping[str, Any] | None, key: str) -> str | None:
    """A metadata value when it is text, as SQLite's json_type(...) = 'text' reads it."""
    value = metadata.get(key) if isinstance(metadata, Mapping) else None
    return value if isinstance(value, str) else None


def item_kind(
    source_type: str,
    metadata: Mapping[str, Any] | None,
    parent_id: int | None,
    held: bool = True,
) -> str:
    """The item's kind: one of KINDS. Mirrors KIND_SQL."""
    child = parent_id is not None
    if not held:
        return "stub"
    if source_type == "linkedin":
        resource = _text(metadata, "resource_name")
        if resource == "socialActions/likes":
            return "reaction"
        if resource == "socialActions/comments":
            return "comment"
        if resource in ("messages", "invitations"):
            return "message"
        if resource in _LINKEDIN_POSTS:
            return "post"
        return "other"
    if source_type in ("bluesky", "microblog"):
        return "comment" if child else "post"
    if source_type == "youtube":
        subtype = _text(metadata, "source_subtype")
        if subtype == "post":
            return "post"
        if subtype == "search":
            return "search"
        if _text(metadata, "type") == "transcript":
            return "transcript"
        return "video"
    if source_type == "arxiv":
        return "paper"
    if source_type == "kobo":
        return "highlight" if _text(metadata, "kind") in _BOOK_PARTS else "book"
    if source_type == "audible":
        return "book" if _text(metadata, "type") == "book" else "highlight"
    if source_type == "jelu":
        return "book"
    if source_type in ("claude", "claude-code", "claude_code", "mistral"):
        return "turn" if child else "chat"
    if source_type == "gemini":
        if _text(metadata, "type") == "canvas":
            return "document"
        return "turn" if child else "chat"
    if source_type == "notebooklm":
        kind = _text(metadata, "type")
        if kind == "notebook":
            return "notebook"
        if kind in ("artifact", "report"):
            return "artifact"
        if kind in ("chat", "note"):
            return kind
        return "document"
    if source_type == "claude-project":
        return "document" if child else "project"
    if source_type == "gmail":
        return "email"
    if source_type == "email":
        return "message" if child else "email"
    if source_type in ("note", "voice", "atomicguard", "claude-memory"):
        return "note"
    if source_type == "image":
        return "image"
    return "document"


def _m(key: str) -> str:
    doc = "CASE WHEN json_valid(metadata) THEN metadata END"
    return f"CASE WHEN json_type({doc}, '$.{key}') = 'text' THEN json_extract({doc}, '$.{key}') END"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# Column names only, no table prefix: a generated column's expression can't name its table
KIND_SQL = f"""CASE
    WHEN held = 0 THEN 'stub'
    WHEN source_type = 'linkedin' THEN CASE
        WHEN {_m("resource_name")} = 'socialActions/likes' THEN 'reaction'
        WHEN {_m("resource_name")} = 'socialActions/comments' THEN 'comment'
        WHEN {_m("resource_name")} IN ('messages', 'invitations') THEN 'message'
        WHEN {_m("resource_name")} IN ({_in(_LINKEDIN_POSTS)}) THEN 'post'
        ELSE 'other' END
    WHEN source_type IN ('bluesky', 'microblog') THEN
        CASE WHEN parent_id IS NULL THEN 'post' ELSE 'comment' END
    WHEN source_type = 'youtube' THEN CASE
        WHEN {_m("source_subtype")} = 'post' THEN 'post'
        WHEN {_m("source_subtype")} = 'search' THEN 'search'
        WHEN {_m("type")} = 'transcript' THEN 'transcript'
        ELSE 'video' END
    WHEN source_type = 'arxiv' THEN 'paper'
    WHEN source_type = 'kobo' THEN
        CASE WHEN {_m("kind")} IN ({_in(_BOOK_PARTS)}) THEN 'highlight' ELSE 'book' END
    WHEN source_type = 'audible' THEN
        CASE WHEN {_m("type")} = 'book' THEN 'book' ELSE 'highlight' END
    WHEN source_type = 'jelu' THEN 'book'
    WHEN source_type IN ('claude', 'claude-code', 'claude_code', 'mistral') THEN
        CASE WHEN parent_id IS NULL THEN 'chat' ELSE 'turn' END
    WHEN source_type = 'gemini' THEN CASE
        WHEN {_m("type")} = 'canvas' THEN 'document'
        WHEN parent_id IS NULL THEN 'chat' ELSE 'turn' END
    WHEN source_type = 'notebooklm' THEN CASE
        WHEN {_m("type")} = 'notebook' THEN 'notebook'
        WHEN {_m("type")} IN ('artifact', 'report') THEN 'artifact'
        WHEN {_m("type")} = 'chat' THEN 'chat'
        WHEN {_m("type")} = 'note' THEN 'note'
        ELSE 'document' END
    WHEN source_type = 'claude-project' THEN
        CASE WHEN parent_id IS NULL THEN 'project' ELSE 'document' END
    WHEN source_type = 'gmail' THEN 'email'
    WHEN source_type = 'email' THEN
        CASE WHEN parent_id IS NULL THEN 'email' ELSE 'message' END
    WHEN source_type IN ('note', 'voice', 'atomicguard', 'claude-memory') THEN 'note'
    WHEN source_type = 'image' THEN 'image'
    ELSE 'document'
END"""


def kind_detail(kind: str, metadata: Mapping[str, Any] | None) -> str | None:
    """A finer label within a kind, in words: the type of a generated artifact."""
    if kind == "artifact":
        artifact = _text(metadata, "artifact_type") or _text(metadata, "type")
        return ARTIFACT_LABELS.get(artifact or "", artifact)
    return None

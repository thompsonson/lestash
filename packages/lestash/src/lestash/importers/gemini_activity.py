"""Gemini chats from a Google Takeout export's Gemini Apps activity.

Takeout's ``My Activity/Gemini Apps/My Activity.json`` (choose JSON for My Activity when
creating the export) has one entry per prompt. Each one names its chat by link, has the
prompt in its title and Gemini's reply as HTML. Chats have no titles in Takeout.

- A chat becomes a ``gemini`` item with ``source_id`` ``c_<chat id>``, the id LeStash
  already stores as ``metadata.conversation_id`` for chats saved from the Gemini page.
- Each prompt and each reply becomes a child turn, with ``items.author`` ``user`` or
  ``model``, as the app's chat view expects. A reply takes its prompt's time, as Takeout
  gives no other; listings put it after the prompt, which is stored first.
- A reply becomes Markdown (markdownify), keeping its links, citations among them.
- Some replies have lines reduced to a lone full stop; the cause is unconfirmed. Such a
  reply is kept verbatim and flagged ``takeout_gaps``, so it can be checked later.
- A prompt that names several chats is stored as a turn of each of them.
- Attachments are listed by file name; the files are not imported.
- A NotebookLM notebook named in a prompt ("X was used in this chat") is kept by name.
- Each Canvas document becomes a top-level ``gemini`` item. Takeout does not say which
  chat it came from, so none is named; its time is the only hint for a later linker.

Feedback, "Used an Assistant feature" and similar entries are not items. They belong in
the planned activities table. merge_with_stored() matches these chats to stored ones.
"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any

from lestash.importers.youtube_history import MAX_TIMES, _time
from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

SOURCE = "takeout_activity"  # metadata.source of what this importer writes
TITLE_LENGTH = 80
# A chat's link; accounts after the first have /u/<n>/ before /app/
CHAT_URL = re.compile(r"^https://gemini\.google\.com/((?:u/\d+/)?)app/([0-9a-f]+)/?(?:[?#].*)?$")
NOTEBOOK = re.compile(r"^(.+?)\s+was used in this chat\.")
GENERATED_IMAGES = re.compile(r"^(\d+) generated images?\.$")
PROMPTED = "Prompted"
CANVAS = "Created Gemini Canvas titled "


def prompt_key(text: str | None) -> str:
    """A prompt's text for matching: without whitespace, since the HTML page parser joins
    inline elements without spaces ("Explain <b>this</b>" becomes "Explainthis")."""
    return "".join((text or "").split())


def chat_source_id(chat_id: str) -> str:
    """The chat's ``source_id``, also its ``metadata.conversation_id``."""
    return f"c_{chat_id}"


def chat_content(prompt_count: int) -> str:
    """Content of a chat item; also rebuilt when a re-import merges prompt times."""
    return f"Gemini conversation with {prompt_count} prompt{'s' if prompt_count != 1 else ''}."


def chat_title(first_prompt: str) -> str:
    title = " ".join(first_prompt.split()) or "Gemini conversation"
    return title[:TITLE_LENGTH] + ("..." if len(title) > TITLE_LENGTH else "")


@cache
def _markdown() -> Any:
    """The reply converter. bs4 and markdownify load here, not at import: merge.py imports
    this module, and so does every CLI start."""
    from markdownify import ATX, MarkdownConverter

    return MarkdownConverter(heading_style=ATX, bullets="-", escape_misc=False)


def _convert(html: str) -> tuple[str, int]:
    """A reply's Markdown and its link count; the links include its citations.

    "|" in a table cell is escaped, as it would end the cell. HTML markdownify can't take
    (such as an ordered list starting at "²") falls back to its plain text.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    links = len(soup.find_all("a", href=True))
    for numbered in soup.find_all("ol", start=True):
        if not str(numbered["start"]).isascii() or not str(numbered["start"]).isdigit():
            del numbered["start"]  # markdownify needs a number
    for string in list(soup.find_all(string=True)):
        if "|" in string and string.find_parent(["td", "th"]):
            string.replace_with(string.replace("|", "\\|"))
    try:
        text = _markdown().convert_soup(soup)
    except Exception:  # noqa: BLE001 - one odd reply must not cost its prompt and chat
        text = BeautifulSoup(html, "html.parser").get_text("\n")
    return text.strip(), links


def html_to_text(html: str) -> str:
    return _convert(html)[0]


# Some Takeout replies have a paragraph or list item that is a lone full stop, or starts
# with one. The cause is unconfirmed; the text is kept as it is and the reply flagged.
TAKEOUT_GAP = re.compile(r"^\s*(?:(?:[-*+]|\d+\.) +|> ?)*\.(?:\s|$)")


def _has_gaps(markdown: str) -> bool:
    """Whether a line outside code blocks starts with a lone full stop."""
    in_code = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and TAKEOUT_GAP.match(line):
            return True
    return False


def _reply(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A reply's Markdown, and its link count and gap flag for its metadata."""
    texts, links = [], 0
    for part in entry.get("safeHtmlItem") or []:
        text, count = _convert(part.get("html") or "")
        if text:
            texts.append(text)
        links += count
    text = "\n\n".join(texts)
    details: dict[str, Any] = {}
    if links:
        details["link_count"] = links
    if _has_gaps(text):
        details["takeout_gaps"] = True
    return text, details


def _chat_ids(entry: dict[str, Any]) -> dict[str, str]:
    """The chats a prompt names: chat id -> its link, keeping the account segment."""
    ids: dict[str, str] = {}
    for detail in entry.get("details") or []:
        match = CHAT_URL.match(detail.get("url") or "")
        if match:
            account, chat_id = match.groups()
            ids.setdefault(chat_id, f"https://gemini.google.com/{account}app/{chat_id}")
    return ids


def _prompt_details(entry: dict[str, Any]) -> dict[str, Any]:
    """What a prompt's subtitles say: attachments, notebook, generated images, audio."""
    details: dict[str, Any] = {}
    attachments = [str(name) for name in entry.get("attachedFiles") or []]
    if entry.get("imageFile") and entry["imageFile"] not in attachments:
        attachments.append(str(entry["imageFile"]))
    for subtitle in entry.get("subtitles") or []:
        name = (subtitle.get("name") or "").strip()
        if match := NOTEBOOK.match(name):
            details["notebooklm_notebook"] = match.group(1).strip()
        elif match := GENERATED_IMAGES.match(name):
            details["generated_images"] = int(match.group(1))
        elif name == "Audio included.":
            details["audio"] = True
    if attachments:
        details["attachments"] = attachments
    return details


@dataclass(frozen=True)
class _Prompt:
    """One "Prompted" entry, parsed once, however many chats it names."""

    when: datetime
    stamp: str  # the time, normalised, part of the turn ids
    text: str
    details: dict[str, Any]
    reply: str
    reply_details: dict[str, Any]


def _utc(when: datetime | None) -> datetime | None:
    """A Takeout time in UTC; one without an offset is taken as UTC, as Takeout writes."""
    if when is None:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _iso(when: datetime) -> str:
    """A time as stored in metadata: UTC to the millisecond, so equal times are equal
    strings, and strings sort as times."""
    return when.isoformat(timespec="milliseconds")


def _stamp(when: datetime) -> str:
    """A time as it goes into ids: UTC, to the millisecond, however the export wrote it."""
    return _iso(when).replace("+00:00", "Z")


def _parse_prompt(entry: dict[str, Any], when: datetime) -> _Prompt:
    reply, reply_details = _reply(entry)
    return _Prompt(
        when,
        _stamp(when),
        _prompt_text(entry),
        _prompt_details(entry),
        reply,
        reply_details,
    )


class _Chat:
    def __init__(self) -> None:
        self.prompts: list[_Prompt] = []
        self.notebooks: list[str] = []
        self.url = ""


def parse_gemini_activity(entries: list[dict[str, Any]]) -> list[ItemCreate]:
    """Chats with their prompt and reply turns, and Canvas documents.

    Each chat comes before its turns, as the sync loop links children to parents
    stored earlier. An entry of an unexpected shape is skipped and counted in the log.
    """
    chats: dict[str, _Chat] = defaultdict(_Chat)
    canvases: list[ItemCreate] = []
    unlinked = malformed = 0
    for entry in entries:
        try:
            if not isinstance(entry, dict) or entry.get("header") != "Gemini Apps":
                continue
            title = entry.get("title")
            when = _utc(_time(entry))
            if not isinstance(title, str) or when is None:
                continue
            if title == PROMPTED or title.startswith(f"{PROMPTED} "):
                chat_ids = _chat_ids(entry)
                if not chat_ids:
                    unlinked += 1
                    continue
                prompt = _parse_prompt(entry, when)
                notebook = prompt.details.get("notebooklm_notebook")
                for chat_id, url in chat_ids.items():
                    chat = chats[chat_id]
                    chat.url = chat.url or url
                    chat.prompts.append(prompt)
                    if notebook and notebook not in chat.notebooks:
                        chat.notebooks.append(notebook)
            elif title.startswith(CANVAS):
                canvases.append(_canvas(entry, when))
        except (AttributeError, KeyError, TypeError, ValueError):
            malformed += 1

    if unlinked:
        logger.warning("Skipped %d Gemini prompts that name no chat", unlinked)
    if malformed:
        logger.warning("Skipped %d Gemini activity entries of an unexpected shape", malformed)
    items: list[ItemCreate] = []
    for chat_id, chat in chats.items():
        # An entry listed twice is one prompt: its turns share their ids
        chat.prompts = sorted({p.stamp: p for p in chat.prompts}.values(), key=lambda p: p.when)
        items.append(_chat_item(chat_id, chat))
        seen: dict[str, int] = defaultdict(int)
        for prompt in chat.prompts:
            seen[prompt_key(prompt.text)] += 1
            items.extend(_turns(chat_id, prompt, seen[prompt_key(prompt.text)]))
    return items + canvases


def _chat_item(chat_id: str, chat: _Chat) -> ItemCreate:
    times = [prompt.when for prompt in chat.prompts]
    metadata: dict[str, Any] = {
        "source": SOURCE,
        "type": "chat",
        "conversation_id": chat_source_id(chat_id),
        "generated_title": chat_title(chat.prompts[0].text),
        "prompted_times": [_iso(t) for t in times[-MAX_TIMES:]],
        "prompted_count": len(times),
        "first_at": _iso(times[0]),
        "last_at": _iso(times[-1]),
    }
    if chat.notebooks:
        metadata["notebooklm_notebooks"] = chat.notebooks
    return ItemCreate(
        source_type="gemini",
        source_id=chat_source_id(chat_id),
        url=chat.url,
        title=chat_title(chat.prompts[0].text),
        content=chat_content(len(times)),
        created_at=times[0],
        is_own_content=True,
        metadata=metadata,
    )


def _prompt_text(entry: dict[str, Any]) -> str:
    return str(entry.get("title") or "").removeprefix(PROMPTED).strip()


def _turns(chat_id: str, prompt: _Prompt, occurrence: int) -> list[ItemCreate]:
    """A prompt and its reply. ``occurrence`` counts the chat's prompts with this text so
    far, so merge_with_stored() can match repeated prompts one to one. It and ``_prompt``
    are dropped before storing."""
    parent = chat_source_id(chat_id)
    prompt_id = f"{parent}-prompt-{prompt.stamp}"
    turns = [
        ItemCreate(
            source_type="gemini",
            source_id=prompt_id,
            content=prompt.text,
            author="user",
            created_at=prompt.when,
            is_own_content=True,
            metadata={
                "source": SOURCE,
                "role": "user",
                **prompt.details,
                "_parent_source_id": parent,
                "_occurrence": occurrence,
            },
        )
    ]
    if prompt.reply:
        turns.append(
            ItemCreate(
                source_type="gemini",
                source_id=f"{parent}-reply-{prompt.stamp}",
                content=prompt.reply,
                author="model",
                created_at=prompt.when,
                is_own_content=False,
                metadata={
                    "source": SOURCE,
                    "role": "model",
                    "prompt_source_id": prompt_id,
                    **prompt.reply_details,
                    "_parent_source_id": parent,
                    "_prompt": prompt.text,
                    "_occurrence": occurrence,
                },
            )
        )
    return turns


def _canvas(entry: dict[str, Any], when: datetime) -> ItemCreate:
    title = entry["title"].removeprefix(CANVAS).strip()
    # Some entries carry the document, some several drafts of it, some nothing
    drafts: list[str] = []
    for subtitle in entry.get("subtitles") or []:
        text = (subtitle.get("name") or "").strip()
        if text and text not in drafts:
            drafts.append(text)
    metadata: dict[str, Any] = {"source": SOURCE, "type": "canvas"}
    if not drafts:
        metadata["content_in_export"] = False
    elif len(drafts) > 1:
        metadata["draft_count"] = len(drafts)
    return ItemCreate(
        source_type="gemini",
        source_id=f"canvas-{_stamp(when)}",
        title=title or "Gemini Canvas",
        content="\n\n---\n\n".join(drafts) if drafts else title,
        created_at=when,
        is_own_content=True,
        metadata=metadata,
    )

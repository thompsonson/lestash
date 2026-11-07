"""Gemini chats from a Google Takeout export's Gemini Apps activity.

Takeout's ``My Activity/Gemini Apps/My Activity.json`` (choose JSON for My Activity when
creating the export) has one entry per prompt. Each one names its chat by link, has the
prompt in its title and Gemini's reply as HTML. Chats have no titles in Takeout.

- A chat becomes a ``gemini`` item with ``source_id`` ``c_<chat id>``, the id LeStash
  already stores as ``metadata.conversation_id`` for chats saved from the Gemini page.
- Each prompt and each reply becomes a child turn, with ``items.author`` ``user`` or
  ``model``, as the app's chat view expects. A reply is dated 1 ms after its prompt, so
  turns sort in order.
- A reply keeps its links, citations among them, as ``[text](url)``.
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
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

SOURCE = "takeout_activity"  # metadata.source of what this importer writes
MAX_TIMES = 200  # prompt times kept on a chat; matches youtube_history.MAX_TIMES
TITLE_LENGTH = 80
# A chat's link; accounts after the first have /u/<n>/ before /app/
CHAT_URL = re.compile(r"^https://gemini\.google\.com/(?:u/\d+/)?app/([0-9a-f]+)/?(?:[?#].*)?$")
NOTEBOOK = re.compile(r"^(.+?)\s+was used in this chat\.")
GENERATED_IMAGES = re.compile(r"^(\d+) generated images?\.$")
PROMPTED = "Prompted "
CANVAS = "Created Gemini Canvas titled "


def chat_source_id(chat_id: str) -> str:
    """The chat's ``source_id``, also its ``metadata.conversation_id``."""
    return f"c_{chat_id}"


def chat_content(prompt_count: int) -> str:
    """Content of a chat item; also rebuilt when a re-import merges prompt times."""
    return f"Gemini conversation with {prompt_count} prompt{'s' if prompt_count != 1 else ''}."


def chat_title(first_prompt: str) -> str:
    title = " ".join(first_prompt.split())
    return title[:TITLE_LENGTH] + ("..." if len(title) > TITLE_LENGTH else "")


def _time(entry: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None


class _TextFromHtml(HTMLParser):
    """Markdown-like text from a reply's HTML.

    Blocks go on their own lines, lists keep their markers and nesting, tables become
    pipe tables, ``<pre>`` becomes a fenced block, and links stay as ``[text](url)``,
    since they include the reply's citations. Other whitespace is collapsed, as a browser
    would.
    """

    BLOCKS = frozenset(
        {"p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}
    )
    VOID = frozenset({"br", "hr"})  # no end tag, though "<br/>" calls handle_endtag
    TEXT_BLOCKS = frozenset({"p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0  # inside script or style
        self.pre = 0
        self.code = 0  # inside pre or code
        self.links: list[tuple[int, str]] = []  # open links: where their text starts, url
        self.link_count = 0
        self.lists: list[list[Any]] = []  # open lists: [tag, items so far]
        self.row: list[str] = []  # cell tags of the open table row
        self.header_done = False  # the open table has its separator line
        self.block_start = False  # a paragraph or list item has no text yet
        self.gaps = False  # see TAKEOUT_GAP

    def _at_line_start(self) -> bool:
        last = self.parts[-1] if self.parts else "\n"
        return last.endswith("\n") or last.endswith(("- ", ". ", "| "))

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.TEXT_BLOCKS:
            self.block_start = True
        if tag in ("pre", "code"):
            self.code += 1
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "pre":
            self.pre += 1
            self._newline()
            self.parts.append("\n```\n")
        elif tag == "a":
            self.links.append((len(self.parts), dict(attrs).get("href") or ""))
        elif tag in ("ul", "ol"):
            if not self.lists:
                self.parts.append("\n")
            self.lists.append([tag, 0])
        elif tag == "li":
            self._newline()
            indent = "  " * max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.parts.append(f"{indent}{self.lists[-1][1]}. ")
            else:
                self.parts.append(f"{indent}- ")
        elif tag == "tr":
            self._newline()
            self.row = []
            self.parts.append("|")
        elif tag in ("td", "th"):
            self.row.append(tag)
            self.parts.append(" ")
        elif tag == "table":
            self.header_done = False
            self.parts.append("\n")
        elif tag in self.BLOCKS and not self._at_item_start():
            self.parts.append("\n")  # "<li><p>x" is one line, "- x"

    def _at_item_start(self) -> bool:
        return bool(self.parts) and self.parts[-1].endswith(("- ", ". "))

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if tag in ("pre", "code"):
            self.code = max(0, self.code - 1)
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._newline()
            self.parts.append("```\n\n")
        elif tag == "a" and self.links:
            start, href = self.links.pop()
            text = " ".join("".join(self.parts[start:]).split())
            if href:
                self.link_count += 1
                self.parts[start:] = [href if text in ("", href) else f"[{text}]({href})"]
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" |")
        elif tag == "tr":
            if self.row and not self.header_done:
                # Markdown needs a separator under the first row
                self.parts.append("\n|" + " --- |" * len(self.row))
                self.header_done = True
            self.parts.append("\n")
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            if self._at_line_start():
                data = data.lstrip()
            if not data:
                return
        if self.block_start and data.strip():
            self.block_start = False
            if not self.code and TAKEOUT_GAP.match(data.lstrip()):
                self.gaps = True
        self.parts.append(data)


def _convert(html: str) -> _TextFromHtml:
    parser = _TextFromHtml()
    parser.feed(html)
    parser.close()
    return parser


def _text(parser: _TextFromHtml) -> str:
    lines = [line.rstrip() for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    return _text(_convert(html))


# Some Takeout replies have a paragraph or list item that is a lone full stop, or starts
# with one. The cause is unconfirmed; the text is kept as it is and the reply flagged.
TAKEOUT_GAP = re.compile(r"\.(?:\s|$)")


def _reply(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A reply's text, and its link count and gap flag for its metadata."""
    parsers = [_convert(part.get("html") or "") for part in entry.get("safeHtmlItem") or []]
    text = "\n\n".join(t for t in map(_text, parsers) if t)
    details: dict[str, Any] = {}
    if links := sum(p.link_count for p in parsers):
        details["link_count"] = links
    if any(p.gaps for p in parsers):
        details["takeout_gaps"] = True
    return text, details


def _chat_ids(entry: dict[str, Any]) -> list[str]:
    ids = []
    for detail in entry.get("details") or []:
        match = CHAT_URL.match(detail.get("url") or "")
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
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
    stamp: str  # Takeout's own time string, part of the turn ids
    text: str
    details: dict[str, Any]
    reply: str
    reply_details: dict[str, Any]


def _parse_prompt(entry: dict[str, Any], when: datetime) -> _Prompt:
    reply, reply_details = _reply(entry)
    return _Prompt(
        when, str(entry["time"]), _prompt_text(entry), _prompt_details(entry), reply, reply_details
    )


class _Chat:
    def __init__(self) -> None:
        self.prompts: list[_Prompt] = []
        self.notebooks: list[str] = []


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
            when = _time(entry)
            if not isinstance(title, str) or when is None:
                continue
            if title.startswith(PROMPTED):
                chat_ids = _chat_ids(entry)
                if not chat_ids:
                    unlinked += 1
                    continue
                prompt = _parse_prompt(entry, when)
                notebook = prompt.details.get("notebooklm_notebook")
                for chat_id in chat_ids:
                    chat = chats[chat_id]
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
        chat.prompts.sort(key=lambda prompt: prompt.when)
        items.append(_chat_item(chat_id, chat))
        seen: dict[str, int] = defaultdict(int)
        for prompt in chat.prompts:
            key = " ".join(prompt.text.split())
            seen[key] += 1
            items.extend(_turns(chat_id, prompt, seen[key]))
    return items + canvases


def _chat_item(chat_id: str, chat: _Chat) -> ItemCreate:
    times = [prompt.when for prompt in chat.prompts]
    metadata: dict[str, Any] = {
        "source": SOURCE,
        "type": "chat",
        "conversation_id": chat_source_id(chat_id),
        "prompted_times": [t.isoformat() for t in times[-MAX_TIMES:]],
        "prompted_count": len(times),
        "first_at": times[0].isoformat(),
        "last_at": times[-1].isoformat(),
    }
    if chat.notebooks:
        metadata["notebooklm_notebooks"] = chat.notebooks
    return ItemCreate(
        source_type="gemini",
        source_id=chat_source_id(chat_id),
        url=f"https://gemini.google.com/app/{chat_id}",
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
                created_at=prompt.when + timedelta(milliseconds=1),
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
        source_id=f"canvas-{entry['time']}",
        title=title or "Gemini Canvas",
        content="\n\n---\n\n".join(drafts) if drafts else title,
        created_at=when,
        is_own_content=True,
        metadata=metadata,
    )

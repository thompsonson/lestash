"""Parse a Claude data export ZIP (claude.ai: Settings → Privacy → Export data) (#88).

- conversations.json: a `claude` item per conversation, with its messages as children.
- projects.json: a `claude-project` item per project, with its documents as children.
- memories.json: `claude-memory` items, the account's memory and each project's.

Items keep the source ids and fields the first import of an export stored, so a newer
export updates those items rather than adding copies.
"""

import contextlib
import json
import logging
import zipfile
from datetime import UTC, datetime

from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

# A conversation with no summary shows the start of its first message
SUMMARY_CHARS = 200
# A message you sent is titled by its start
TITLE_CHARS = 80
EMPTY_CONVERSATION = "(empty conversation)"
NO_DESCRIPTION = "(no description)"
PROMPT_HEADING = "\n\n---\n\n**Prompt template:**\n"


def detect_claude_zip(names: list[str]) -> bool:
    """A Claude export holds conversations.json and users.json at its root.

    A ChatGPT export also has conversations.json, but user.json, not users.json.
    """
    return "conversations.json" in names and "users.json" in names


def parse_claude_zip(zf: zipfile.ZipFile) -> list[ItemCreate]:
    """Every conversation, project and memory in the export, parents before children."""
    names = set(zf.namelist())
    items: list[ItemCreate] = []
    for conversation in _read_list(zf, "conversations.json"):
        items.extend(_conversation(conversation))
    if "projects.json" in names:
        for project in _read_list(zf, "projects.json"):
            items.extend(_project(project))
    if "memories.json" in names:
        for memory in _read_list(zf, "memories.json"):
            items.extend(_memories(memory))
    return items


def _read_list(zf: zipfile.ZipFile, name: str) -> list[dict]:
    try:
        data = json.loads(zf.read(name))
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        logger.warning("Could not read %s in the Claude export", name)
        return []
    if not isinstance(data, list):
        logger.warning("%s in the Claude export is not a list", name)
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _when(value: object) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return None


def _text(message: dict) -> str:
    """The message's text; an export without `text` has it in its content blocks."""
    text = message.get("text") or ""
    if text.strip():
        return text
    blocks = message.get("content") or []
    return "\n\n".join(
        b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )


def _conversation(conversation: dict) -> list[ItemCreate]:
    uuid = conversation.get("uuid")
    if not uuid:
        logger.warning("Skipping a Claude conversation with no uuid")
        return []
    messages = [m for m in conversation.get("chat_messages") or [] if isinstance(m, dict)]
    children = [child for m in messages if (child := _message(m, uuid))]
    first = next((t for m in messages if (t := _text(m)).strip()), "")
    parent = ItemCreate(
        source_type="claude",
        source_id=uuid,
        title=conversation.get("name"),  # an untitled one is stored with an empty title
        content=conversation.get("summary") or first[:SUMMARY_CHARS] or EMPTY_CONVERSATION,
        created_at=_when(conversation.get("created_at")),
        is_own_content=True,
        metadata={
            "source": "export",
            "message_count": len(messages),
            "has_attachments": any(m.get("attachments") for m in messages),
            "has_files": any(m.get("files") for m in messages),
        },
    )
    return [parent, *children]


def _message(message: dict, conversation_uuid: str) -> ItemCreate | None:
    uuid = message.get("uuid")
    text = _text(message)
    if not uuid or not text.strip():
        return None  # tool calls and thinking only: nothing to read
    sender = message.get("sender") or "unknown"
    own = sender == "human"
    metadata: dict[str, object] = {"sender": sender, "_parent_source_id": conversation_uuid}
    attachments = [a for a in message.get("attachments") or [] if isinstance(a, dict)]
    if attachments:
        # The extracted text stays out: the message already quotes what matters
        metadata["attachments"] = [
            {key: a.get(key) for key in ("file_name", "file_size", "file_type")}
            for a in attachments
        ]
    files = [f.get("file_name") for f in message.get("files") or [] if isinstance(f, dict)]
    if files:
        metadata["files"] = files
    title = None
    if own:
        title = text[:TITLE_CHARS] + ("..." if len(text) > TITLE_CHARS else "")
    return ItemCreate(
        source_type="claude",
        source_id=uuid,
        title=title,
        content=text,
        created_at=_when(message.get("created_at")),
        is_own_content=own,
        metadata=metadata,
    )


def _project(project: dict) -> list[ItemCreate]:
    uuid = project.get("uuid")
    if not uuid:
        logger.warning("Skipping a Claude project with no uuid")
        return []
    docs = [d for d in project.get("docs") or [] if isinstance(d, dict)]
    content = project.get("description") or ""
    if project.get("prompt_template"):
        content += PROMPT_HEADING + project["prompt_template"]
    content = content or NO_DESCRIPTION
    items = [
        ItemCreate(
            source_type="claude-project",
            source_id=uuid,
            title=project.get("name") or None,
            content=content,
            created_at=_when(project.get("created_at")),
            is_own_content=True,
            metadata={
                "source": "export",
                "doc_count": len(docs),
                "is_private": project.get("is_private"),
            },
        )
    ]
    for doc in docs:
        if not doc.get("uuid") or not (doc.get("content") or "").strip():
            continue
        items.append(
            ItemCreate(
                source_type="claude-project",
                source_id=doc["uuid"],
                title=doc.get("filename") or None,
                content=doc["content"],
                created_at=_when(doc.get("created_at")),
                is_own_content=True,
                metadata={"source": "export", "type": "doc", "_parent_source_id": uuid},
            )
        )
    return items


def _memories(memory: dict) -> list[ItemCreate]:
    """Memories carry no date, so they are dated when imported."""
    now = datetime.now(UTC)
    items = []
    if (memory.get("conversations_memory") or "").strip():
        items.append(
            ItemCreate(
                source_type="claude-memory",
                source_id="conversations-memory",
                title="Conversation Memory",
                content=memory["conversations_memory"],
                created_at=now,
                is_own_content=True,
                metadata={"source": "export", "type": "conversations"},
            )
        )
    for project_uuid, text in (memory.get("project_memories") or {}).items():
        if not isinstance(text, str) or not text.strip():
            continue
        items.append(
            ItemCreate(
                source_type="claude-memory",
                source_id=f"project-memory-{project_uuid}",
                title=f"Project Memory ({project_uuid[:8]}...)",
                content=text,
                created_at=now,
                is_own_content=True,
                metadata={"source": "export", "type": "project", "project_uuid": project_uuid},
            )
        )
    return items

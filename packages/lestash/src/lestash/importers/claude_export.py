"""Parse a Claude data export ZIP (claude.ai: Settings → Privacy → Export data) (#88).

- conversations.json: a `claude` item per conversation, with its messages as children.
- projects: a `claude-project` item per project, with its documents as children.
- memories.json: `claude-memory` items, the account's memory and each project's.

Two layouts:
- One ZIP holding conversations.json, users.json, projects.json and memories.json
  (the export until 2026).
- A manifest of ZIPs, one per part (from November 2026): `conversations-000.zip` holds
  conversations.json alone; `projects-000.zip` holds `projects/<uuid>.json`, one project
  each. The other parts import nothing here and say why: `light_metadata` (account and
  login history), `frames` and `artifact_data` (Artifacts, #437) and `claude_code_cloud`
  (routines, #439).

Items keep the source ids and fields the first import of an export stored, so a newer
export updates those items rather than adding copies.
"""

import contextlib
import json
import logging
import re
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


PROJECT_FILE = re.compile(r"^projects/[^/]+\.json$")
ARTIFACT_FILE = re.compile(r"^artifacts/[^/]+/(artifact\.json|data/documents\.json)$")


class NothingToImport(ValueError):
    """A part of a Claude export that LeStash reads nothing from (yet)."""


def detect_claude_zip(names: list[str]) -> bool:
    """Whether the ZIP may be a Claude export, or a part of one, by its file names.

    ``parse_claude_zip`` then checks the content, so a ZIP of the same names from
    elsewhere (a ChatGPT export's conversations.json) is left to the other importers.
    """
    found = set(names)
    return bool(
        found & {"conversations.json", "projects.json", "memories.json", "login_history.json"}
        or "claude_code_cloud/routines.json" in found
        or any(PROJECT_FILE.match(n) or ARTIFACT_FILE.match(n) for n in found)
    )


def parse_claude_zip(zf: zipfile.ZipFile) -> list[ItemCreate] | None:
    """Every conversation, project and memory in the export, parents before children.

    Each file is taken only if it has Claude's shape. None if nothing in the ZIP does,
    so the other importers can try it. Raises NothingToImport for a part of a Claude
    export LeStash reads nothing from.
    """
    names = set(zf.namelist())
    conversations = [
        c
        for c in (_read_list(zf, "conversations.json") if "conversations.json" in names else [])
        # A ChatGPT export's conversations have a `mapping`, not `chat_messages`
        if "uuid" in c and "chat_messages" in c
    ]
    projects = _read_list(zf, "projects.json") if "projects.json" in names else []
    projects += [
        p for name in sorted(filter(PROJECT_FILE.match, names)) if (p := _read_dict(zf, name))
    ]
    projects = [p for p in projects if "uuid" in p and "docs" in p]
    memories = [
        m
        for m in (_read_list(zf, "memories.json") if "memories.json" in names else [])
        if "conversations_memory" in m or "project_memories" in m
    ]
    if not (conversations or projects or memories):
        why = _not_imported(zf, names)
        if why:
            raise NothingToImport(why)
        return None

    items: list[ItemCreate] = []
    entries = [
        *((_conversation, c) for c in conversations),
        *((_project, p) for p in projects),
        *((_memories, m) for m in memories),
    ]
    for convert, entry in entries:
        # One malformed entry is skipped, never the whole export
        try:
            items.extend(convert(entry))
        except (ValueError, TypeError, AttributeError):
            logger.warning("Skipping a malformed entry in the Claude export", exc_info=True)
    return items


def _not_imported(zf: zipfile.ZipFile, names: set[str]) -> str | None:
    """Why a Claude export, or a part of one, imports nothing, if it is one."""
    lists = [n for n in ("conversations.json", "projects.json", "memories.json") if n in names]
    if lists and all(_read_json(zf, n) == [] for n in lists):
        return "This Claude export is empty: it holds no conversations, projects or memories."
    if "users.json" in names and "login_history.json" in names:
        history = _read_dict(zf, "login_history.json")
        if history is not None and "login_events" in history:
            return (
                "This part of the Claude export holds account details and login history"
                " only; nothing in it is imported."
            )
    artifact = next((n for n in sorted(names) if ARTIFACT_FILE.match(n)), None)
    if artifact:
        data = _read_dict(zf, artifact) or {}
        if {"versions", "active_version"} <= data.keys() or "documents" in data:
            return "Artifacts from the Claude export aren't imported yet (#437)."
    if "claude_code_cloud/routines.json" in names:
        routines = _read_dict(zf, "claude_code_cloud/routines.json")
        if routines is not None and "claude_code_cloud" in routines:
            return "Claude Code routines from the Claude export aren't imported yet (#439)."
    return None


def _read_json(zf: zipfile.ZipFile, name: str) -> object:
    try:
        return json.loads(zf.read(name))
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        logger.warning("Could not read %s in the Claude export", name)
        return None


def _read_list(zf: zipfile.ZipFile, name: str) -> list[dict]:
    data = _read_json(zf, name)
    if not isinstance(data, list):
        if data is not None:
            logger.warning("%s in the Claude export is not a list", name)
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _read_dict(zf: zipfile.ZipFile, name: str) -> dict | None:
    data = _read_json(zf, name)
    return data if isinstance(data, dict) else None


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
    account = memory.get("conversations_memory")
    if isinstance(account, str) and account.strip():
        items.append(
            ItemCreate(
                source_type="claude-memory",
                source_id="conversations-memory",
                title="Conversation Memory",
                content=account,
                created_at=now,
                is_own_content=True,
                metadata={"source": "export", "type": "conversations"},
            )
        )
    projects = memory.get("project_memories")
    for project_uuid, text in (projects if isinstance(projects, dict) else {}).items():
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

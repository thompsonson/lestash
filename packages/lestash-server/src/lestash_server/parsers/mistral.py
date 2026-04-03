"""Parse Mistral Le Chat export ZIP into conversation items."""

import contextlib
import json
import logging
import zipfile
from datetime import datetime

from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)


def detect_mistral_zip(names: list[str]) -> bool:
    """Check if ZIP contains Mistral chat export files."""
    json_files = [n for n in names if n.endswith(".json")]
    if not json_files:
        return False

    # Mistral exports have chat-<uuid>.json filenames
    return any(n.startswith("chat-") and len(n) > 41 for n in json_files)  # chat- + uuid + .json


def parse_mistral_zip(zf: zipfile.ZipFile) -> list[ItemCreate]:
    """Parse Mistral chat export into parent/child conversation items.

    Each JSON file is a conversation (array of messages).
    The conversation becomes a parent item (summary + first user message as title).
    Each message becomes a child item linked via _parent_source_id.
    """
    names = zf.namelist()
    json_files = sorted(n for n in names if n.endswith(".json"))

    items: list[ItemCreate] = []
    for name in json_files:
        try:
            messages = json.loads(zf.read(name))
            if not isinstance(messages, list) or not messages:
                continue

            chat_id = messages[0].get("chatId", name)
            parent_source_id = f"mistral-{chat_id}"

            first_user_msg = ""
            earliest_ts: datetime | None = None
            msg_count = 0

            # Gather conversation-level info and create child items
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if not content:
                    continue

                msg_count += 1
                msg_id = msg.get("id", f"{chat_id}-{msg_count}")

                if role == "user" and not first_user_msg:
                    first_user_msg = content

                ts: datetime | None = None
                ts_str = msg.get("createdAt")
                if ts_str:
                    with contextlib.suppress(ValueError, OSError):
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    if not earliest_ts:
                        earliest_ts = ts

                items.append(
                    ItemCreate(
                        source_type="mistral",
                        source_id=f"mistral-msg-{msg_id}",
                        title=None,
                        content=content,
                        author=role,
                        created_at=ts,
                        is_own_content=(role == "user"),
                        metadata={
                            "role": role,
                            "chat_id": chat_id,
                            "_parent_source_id": parent_source_id,
                        },
                    )
                )

            if msg_count == 0:
                continue

            title = first_user_msg[:80]
            if len(first_user_msg) > 80:
                title += "..."

            # Check for attached files
            file_dir = name.replace(".json", "-files/")
            attached = [
                n.split("/")[-1] for n in names if n.startswith(file_dir) and not n.endswith("/")
            ]

            parent_metadata: dict[str, object] = {
                "source": "mistral",
                "chat_id": chat_id,
                "message_count": msg_count,
            }
            if attached:
                parent_metadata["attached_files"] = attached

            # Build summary content for the parent
            summary_parts = [f"Mistral conversation with {msg_count} messages."]
            if attached:
                summary_parts.append(f"Attached files: {', '.join(attached)}")

            # Insert parent BEFORE children in the list
            items.insert(
                len(items) - msg_count,
                ItemCreate(
                    source_type="mistral",
                    source_id=parent_source_id,
                    title=title or None,
                    content="\n".join(summary_parts),
                    created_at=earliest_ts,
                    is_own_content=True,
                    metadata=parent_metadata,
                ),
            )
        except Exception:
            logger.warning("Failed to parse Mistral chat: %s", name, exc_info=True)
            continue

    return items

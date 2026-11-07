"""Zip importers: Google Takeout (Keep, Gemini, NotebookLM, YouTube history), Mistral, JSON.

Shared by the server's import routes and the Takeout source plugin.
"""

import contextlib
import json
import logging
import re
import zipfile
from datetime import UTC
from io import BytesIO
from pathlib import Path

from lestash.importers.gemini_activity import parse_gemini_activity
from lestash.importers.json_items import parse_json_items
from lestash.importers.mistral import detect_mistral_zip, parse_mistral_zip
from lestash.importers.youtube_history import parse_search_history, parse_watch_history

logger = logging.getLogger(__name__)

# Bump when parse_zip learns a new format, so zips it found nothing in are tried again.
#   1: Keep, Gemini, NotebookLM, Mistral, JSON
#   2: + YouTube watch/search history; several Takeout products per zip
#   3: + Gemini Apps activity (chats with prompt and reply turns, Canvas documents)
IMPORTER_VERSION = 3


class CorruptZip(ValueError):
    """The file is not a readable zip (e.g. a truncated download), as opposed to a zip
    with nothing recognisable inside."""


def parse_zip(data: bytes | Path):
    """Parse a ZIP file (bytes or a path on disk), detecting the format from contents.

    Returns (items, source_type). Raises ValueError if nothing recognizable is inside,
    and its subclass CorruptZip if the file can't be read as a zip.
    """
    try:
        with zipfile.ZipFile(BytesIO(data) if isinstance(data, bytes) else data) as zf:
            names = zf.namelist()

            # Google Takeout: one export can hold several products, so collect them all
            keep_files = [n for n in names if n.startswith("Keep/") and n.endswith(".json")]
            if not keep_files:
                keep_files = [n for n in names if "/Keep/" in n and n.endswith(".json")]
            gemini_convos = [
                n
                for n in names
                if ("Gemini" in n)
                and (n.endswith(".txt") or n.endswith(".json"))
                and "Conversation" in n
                and not n.endswith("/")
            ]
            nlm_files = [n for n in names if "NotebookLM" in n and not n.endswith("/")]
            youtube_history = [n for n in names if YOUTUBE_HISTORY.match(n)]
            gemini_activity = [n for n in names if GEMINI_ACTIVITY.match(n)]

            found: list[tuple[str, list]] = []
            if keep_files:
                found.append(("google-keep", _parse_google_keep_zip(zf, keep_files)))
            if gemini_convos:
                found.append(("gemini", _parse_gemini_zip(zf, gemini_convos)))
            if nlm_files:
                found.append(("notebooklm", _parse_notebooklm_zip(zf, nlm_files, names)))
            if youtube_history:
                found.append(("youtube", _parse_youtube_history_zip(zf, youtube_history)))
            if gemini_activity:
                found.append(("gemini", _parse_gemini_activity_zip(zf, gemini_activity)))
            if found:
                all_items = [item for _, items in found for item in items]
                return all_items, found[0][0] if len(found) == 1 else "takeout"

            # Detect Mistral Le Chat export
            if detect_mistral_zip(names):
                return parse_mistral_zip(zf), "mistral"

            # Try generic JSON files in zip
            json_files = [n for n in names if n.endswith(".json")]
            if json_files:
                all_items = []
                for jf in json_files:
                    try:
                        all_items.extend(parse_json_items(zf.read(jf)))
                    except ValueError:
                        continue
                if all_items:
                    return all_items, "json"

            raise ValueError("ZIP file does not contain recognizable import data")
    except zipfile.BadZipFile:
        raise CorruptZip("Invalid ZIP file") from None


# Your own history only: supervised accounts live under .../children/<name>/.
# The folder and file names (and the "Watched"/"Searched for" title prefixes parsed in
# youtube_history) are those of an English-language Takeout; other languages translate them.
YOUTUBE_HISTORY = re.compile(r"^Takeout/YouTube[^/]*/history/(watch|search)-history\.json$")


# Gemini Apps activity, exported as JSON. Like YOUTUBE_HISTORY, the names are an
# English-language Takeout's; some exports drop the space in the file name.
GEMINI_ACTIVITY = re.compile(r"^Takeout/My Activity/Gemini Apps/My ?Activity\.json$")


def _parse_gemini_activity_zip(zf, activity_files):
    """Gemini chats and Canvas documents from Gemini Apps activity (JSON)."""
    items = []
    for name in activity_files:
        try:
            entries = json.loads(zf.read(name))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Could not read Gemini activity file: {name}")
            continue
        if isinstance(entries, list):
            items.extend(parse_gemini_activity(entries))
    return items


def _parse_youtube_history_zip(zf, history_files):
    """YouTube watch/search history (JSON)."""
    items = []
    for name in history_files:
        try:
            entries = json.loads(zf.read(name))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Could not read YouTube history file: {name}")
            continue
        if not isinstance(entries, list):
            continue
        if name.endswith("watch-history.json"):
            items.extend(parse_watch_history(entries))
        else:
            items.extend(parse_search_history(entries))
    return items


def _parse_google_keep_zip(zf, keep_files):
    """Parse Google Keep notes from a Takeout ZIP."""
    from lestash.models.item import ItemCreate

    items = []
    for name in keep_files:
        try:
            note = json.loads(zf.read(name))
            if note.get("isTrashed", False):
                continue

            content = note.get("textContent", "")
            if not content and note.get("listContent"):
                lines = []
                for li in note["listContent"]:
                    check = "[x]" if li.get("isChecked") else "[ ]"
                    lines.append(f"{check} {li.get('text', '')}")
                content = "\n".join(lines)

            if not content:
                continue

            # Convert microsecond timestamps
            created_us = note.get("createdTimestampUsec", 0)
            created_at = None
            if created_us:
                from datetime import datetime

                created_at = datetime.fromtimestamp(created_us / 1_000_000, tz=UTC)

            metadata: dict[str, object] = {}
            if note.get("labels"):
                metadata["labels"] = [lbl["name"] for lbl in note["labels"]]
            if note.get("color") and note["color"] != "DEFAULT":
                metadata["color"] = note["color"]
            if note.get("isPinned"):
                metadata["is_pinned"] = True
            if note.get("listContent"):
                metadata["list_items"] = [
                    {"text": li.get("text", ""), "is_checked": li.get("isChecked", False)}
                    for li in note["listContent"]
                ]

            items.append(
                ItemCreate(
                    source_type="google-keep",
                    source_id=name,
                    title=note.get("title"),
                    content=content,
                    created_at=created_at,
                    is_own_content=True,
                    metadata=metadata if metadata else None,
                )
            )
        except Exception:
            continue

    return items


def _parse_gemini_zip(zf, gemini_files):
    """Parse Gemini conversations from a Google Takeout ZIP.

    Gemini Takeout exports conversations as .txt files containing JSON with
    'conversation_turns' array of user_turn/system_turn pairs.
    """
    from datetime import datetime

    from lestash.models.item import ItemCreate

    items = []
    for name in gemini_files:
        try:
            raw = zf.read(name).decode("utf-8")
            data = json.loads(raw)
            turns = data.get("conversation_turns", [])
            if not turns:
                continue

            parts = []
            first_prompt = ""
            earliest_ts = None

            for turn in turns:
                if "user_turn" in turn:
                    ut = turn["user_turn"]
                    prompt = ut.get("prompt", "")
                    if prompt:
                        parts.append(f"**User:** {prompt}")
                        if not first_prompt:
                            first_prompt = prompt
                    ts = ut.get("turn_last_modified")
                    if ts and not earliest_ts:
                        earliest_ts = ts

                if "system_turn" in turn:
                    st = turn["system_turn"]
                    text_parts = st.get("text", [])
                    text = "\n".join(t.get("data", "") for t in text_parts if isinstance(t, dict))
                    if text:
                        parts.append(f"**Gemini:** {text}")

            if not parts:
                continue

            created_at = None
            if earliest_ts:
                with contextlib.suppress(ValueError, OSError):
                    created_at = datetime.fromisoformat(str(earliest_ts).replace("Z", "+00:00"))

            title = first_prompt[:80] + ("..." if len(first_prompt) > 80 else "")

            items.append(
                ItemCreate(
                    source_type="gemini",
                    source_id=name,
                    title=title or None,
                    content="\n\n".join(parts),
                    created_at=created_at,
                    is_own_content=True,
                    metadata={"source": "takeout", "turn_count": len(turns)},
                )
            )
        except Exception:
            logger.warning(f"Failed to parse Gemini file: {name}", exc_info=True)
            continue

    return items


def _notebooklm_artifact_labels(zf, nb_dir: str, all_names: list[str]) -> list[str]:
    """Readable names of a notebook's generated artifacts, e.g. "Planning (audio overview)".

    Each artifact has a JSON description with its title and type; the media files beside it
    (wav, mp4, pptx, numbered slide images) are not listed separately.
    """
    labels: set[str] = set()
    prefix = f"{nb_dir}/Artifacts/"
    for name in all_names:
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        try:
            data = json.loads(zf.read(name))
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or not data.get("title"):
            continue
        kind = str(data.get("type", "")).removeprefix("ARTIFACT_TYPE_").replace("_", " ").lower()
        labels.add(f"{data['title']} ({kind})" if kind else data["title"])
    return sorted(labels)


def notebook_summary(title: str, metadata: dict) -> str:
    """Content of a NotebookLM notebook item, from its (possibly merged) metadata."""
    sources = metadata.get("sources") or []
    artifacts = metadata.get("artifacts") or []
    parts = [f"# {title}"]
    if sources:
        parts.append(f"**Sources ({len(sources)}):** " + ", ".join(sources))
    if artifacts:
        parts.append(f"**Generated ({len(artifacts)}):** " + ", ".join(artifacts))
    parts.append(
        f"**Notes:** {metadata.get('note_count', 0)} | **Chats:** {metadata.get('chat_count', 0)}"
    )
    return "\n\n".join(parts)


def notebook_child_name(path: str) -> str:
    """A note, report or chat's name: its file name without the extension."""
    name = path.rsplit("/", 1)[-1]
    return name.removesuffix(".md") if name.endswith(".md") else name.replace(".html", "")


def _parse_notebooklm_zip(zf, nlm_files, all_names):
    """Parse NotebookLM notebooks from a Google Takeout ZIP.

    Structure: NotebookLM/<notebook-title>/
      - <notebook-title>.json (title, emoji, metadata)
      - Notes/*.html (generated notes)
      - Chat History/*.html (chat sessions)
      - Sources/*.html (source content)
      - Sources/*metadata.json (source metadata)
    """
    import re
    from datetime import datetime

    from lestash.models.item import ItemCreate

    # Discover notebook directories
    nb_dirs: set[str] = set()
    for name in nlm_files:
        match = re.match(r"(Takeout/NotebookLM/[^/]+)/", name)
        if match:
            nb_dirs.add(match.group(1))

    items = []
    for nb_dir in sorted(nb_dirs):
        try:
            nb_name = nb_dir.rsplit("/", 1)[-1]

            # Find metadata JSON (not from Sources/Notes/Chat/Artifacts)
            meta_data: dict = {}
            meta_candidates = [
                n
                for n in all_names
                if n.startswith(f"{nb_dir}/")
                and n.endswith(".json")
                and "/Sources/" not in n
                and "/Notes/" not in n
                and "/Chat History/" not in n
                and "/Artifacts/" not in n
            ]
            for m in meta_candidates:
                try:
                    meta_data = json.loads(zf.read(m))
                    break
                except (json.JSONDecodeError, KeyError):
                    continue

            title = meta_data.get("title", nb_name)
            metadata_inner = meta_data.get("metadata", {})

            # Collect content HTML files
            note_htmls = sorted(
                n for n in all_names if n.startswith(f"{nb_dir}/Notes/") and n.endswith(".html")
            )
            chat_htmls = sorted(
                n
                for n in all_names
                if n.startswith(f"{nb_dir}/Chat History/") and n.endswith(".html")
            )
            source_metas = [
                n for n in all_names if n.startswith(f"{nb_dir}/Sources/") and n.endswith(".json")
            ]
            artifact_names = _notebooklm_artifact_labels(zf, nb_dir, all_names)
            artifact_reports = sorted(
                n for n in all_names if n.startswith(f"{nb_dir}/Artifacts/") and n.endswith(".md")
            )

            # Multi-part Takeouts spread a notebook over several zips. Every part yields the
            # notebook item with what it holds; merge_with_stored() combines the parts, and
            # only the part with the notebook's metadata JSON sets its title and date.
            if (
                not any((meta_candidates, note_htmls, chat_htmls, source_metas, artifact_names))
                and not artifact_reports
            ):
                continue

            created_at = None
            create_time = metadata_inner.get("createTime", "")
            if create_time:
                with contextlib.suppress(ValueError, OSError):
                    created_at = datetime.fromisoformat(str(create_time).replace("Z", "+00:00"))

            source_titles = []
            for sm in source_metas:
                try:
                    sd = json.loads(zf.read(sm))
                    if sd.get("title"):
                        source_titles.append(sd["title"])
                except Exception:
                    continue

            # Parent item: notebook overview
            note_names = [n.rsplit("/", 1)[-1].removesuffix(".html") for n in note_htmls]
            chat_names = [n.rsplit("/", 1)[-1].removesuffix(".html") for n in chat_htmls]
            parent_metadata: dict[str, object] = {
                "source": "takeout",
                "type": "notebook",
                "has_metadata": bool(meta_data),
                "note_count": len(note_names),
                "chat_count": len(chat_names),
            }
            if note_names:
                parent_metadata["notes"] = note_names
            if chat_names:
                parent_metadata["chats"] = chat_names
            if meta_data.get("emoji"):
                parent_metadata["emoji"] = meta_data["emoji"]
            if source_titles:
                parent_metadata["sources"] = source_titles
                parent_metadata["source_count"] = len(source_titles)
            if artifact_names:
                parent_metadata["artifacts"] = artifact_names

            items.append(
                ItemCreate(
                    source_type="notebooklm",
                    source_id=nb_dir,
                    title=title,
                    content=notebook_summary(title, parent_metadata),
                    created_at=created_at,
                    is_own_content=True,
                    metadata=parent_metadata,
                )
            )

            # Child items: individual notes
            for nh in note_htmls:
                note_name = notebook_child_name(nh)
                html = zf.read(nh).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "", html).strip()
                if text:
                    items.append(
                        ItemCreate(
                            source_type="notebooklm",
                            source_id=nh,
                            title=f"{title} — {note_name}",
                            content=text,
                            created_at=created_at,
                            is_own_content=True,
                            metadata={
                                "source": "takeout",
                                "type": "note",
                                "_parent_source_id": nb_dir,
                            },
                        )
                    )

            # Child items: generated text reports (briefings, study guides, ...)
            for rp in artifact_reports:
                text = zf.read(rp).decode("utf-8", errors="replace").strip()
                if text:
                    items.append(
                        ItemCreate(
                            source_type="notebooklm",
                            source_id=rp,
                            title=f"{title} — {notebook_child_name(rp)}",
                            content=text,
                            created_at=created_at,
                            is_own_content=True,
                            metadata={
                                "source": "takeout",
                                "type": "report",
                                "_parent_source_id": nb_dir,
                            },
                        )
                    )

            # Child items: individual chat sessions
            for ch in chat_htmls:
                chat_name = notebook_child_name(ch)
                html = zf.read(ch).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "", html).strip()
                if text:
                    text = text.replace("MODEL:", "**Gemini:**")
                    items.append(
                        ItemCreate(
                            source_type="notebooklm",
                            source_id=ch,
                            title=f"{title} — {chat_name}",
                            content=text,
                            created_at=created_at,
                            is_own_content=True,
                            metadata={
                                "source": "takeout",
                                "type": "chat",
                                "_parent_source_id": nb_dir,
                            },
                        )
                    )
        except Exception:
            logger.warning(f"Failed to parse NotebookLM notebook: {nb_dir}", exc_info=True)
            continue

    return items

"""NotebookLM notebooks held twice: one notebook under two folder names.

A notebook's items are keyed by its Takeout folder (``Takeout/NotebookLM/<name>``), the
only key every part of a multi-part export shares. The folder name is not stable:

- Exports before November 2026 cut each folder name to 50 characters, and each file
  name to 51 with its extension; later ones give them in full. The same notebook
  imported from both made two notebook items, each with its own notes, reports, chats
  and artifacts.
- Renaming a notebook renames its folder.

Two notebook items are the same notebook when:

- they share the stable id from the notebook's metadata JSON (``metadata.notebook_id``);
- or they have the same creation time (to the microsecond) and title, for those stored
  before the id was kept;
- or one's folder is among the other's earlier folders (``metadata.folders``), which a
  merge records. An old export's part imported again folds in by that, even one with
  no metadata JSON, so no date or title.

Which copy is current needs evidence too:

- both carry ``metadata.last_viewed`` from their export, and one is later;
- or one's folder is the cut form of the other's (50 characters, beginning it);
- or one's folder is among the other's earlier folders.

A group with no copy shown current by all the others is left alone and logged.

The older copy is merged into the current one (see ``merge_item``): its links, tags,
collections and children move, then it is deleted. A moved child that is the copy of
one the notebook already has (same kind and name, or a cut name the other's begins
with) is merged into it the same way. ``dedupe_notebooklm`` runs after every import
that stores NotebookLM notebooks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, NamedTuple

from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.stubs import merge_item

logger = logging.getLogger(__name__)

CHILD_TYPES = ("note", "report", "chat", "artifact")
FOLDERS = "Takeout/NotebookLM/"
CUT_FOLDER = 50  # characters older exports kept of a folder name
CUT_FILE = 51  # ... and of a file name, its extension included
METADATA = " metadata"


class Duplicate(NamedTuple):
    old_id: int  # merged into current_id, then deleted
    current_id: int  # kept
    title: str


class _Notebook(NamedTuple):
    id: int
    source_id: str
    title: str
    created_at: str | None
    notebook_id: str | None
    last_viewed: datetime | None  # from its export: later in a later export
    folders: tuple[str, ...]  # earlier folders of copies merged into it

    def cut_of(self, other: _Notebook) -> bool:
        """Whether this folder is the cut form of ``other``'s."""
        folder = self.source_id.removeprefix(FOLDERS)
        return (
            self.source_id.startswith(FOLDERS)
            and len(folder) == CUT_FOLDER
            and len(other.source_id) > len(self.source_id)
            and other.source_id.startswith(self.source_id)
        )

    def beats(self, other: _Notebook) -> bool:
        """Whether there is evidence this copy is more current than ``other``."""
        if self.last_viewed and other.last_viewed and self.last_viewed != other.last_viewed:
            return self.last_viewed > other.last_viewed
        return other.cut_of(self) or other.source_id in self.folders


def _viewed(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _metadata(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _notebooks(conn: sqlite3.Connection) -> list[_Notebook]:
    rows = conn.execute(
        "SELECT id, source_id, title, created_at, metadata FROM items "
        "WHERE source_type = 'notebooklm' AND parent_id IS NULL AND held = 1"
    ).fetchall()
    notebooks = []
    for id_, source_id, title, created_at, raw in rows:
        metadata = _metadata(raw)
        if metadata.get("type") != "notebook":
            continue
        notebooks.append(
            _Notebook(
                int(id_),
                str(source_id or ""),
                (title or "").strip(),
                str(created_at) if created_at else None,
                metadata.get("notebook_id") or None,
                _viewed(metadata.get("last_viewed")),
                tuple(f for f in metadata.get("folders") or () if isinstance(f, str)),
            )
        )
    return notebooks


def find_duplicates(conn: sqlite3.Connection) -> list[Duplicate]:
    """Pairs of notebook items that are one notebook, each older copy with the current
    one of its group."""
    notebooks = _notebooks(conn)
    groups: dict[tuple[str, ...], list[_Notebook]] = defaultdict(list)
    for nb in notebooks:
        if nb.notebook_id:
            groups[("id", nb.notebook_id)].append(nb)
        if nb.created_at and nb.title:
            groups[("made", nb.created_at, nb.title)].append(nb)
        for folder in (nb.source_id, *nb.folders):
            groups[("folder", folder)].append(nb)

    # Join groups that share an item (one copy has the id, the other only the time)
    parent: dict[int, int] = {}

    def root(i: int) -> int:
        while parent.get(i, i) != i:
            i = parent[i]
        return i

    for members in groups.values():
        for nb in members[1:]:
            parent[root(nb.id)] = root(members[0].id)
    clusters: dict[int, list[_Notebook]] = defaultdict(list)
    for nb in notebooks:
        clusters[root(nb.id)].append(nb)

    pairs = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        shown = [c for c in members if all(c.beats(o) for o in members if o.id != c.id)]
        if len(shown) != 1:
            logger.warning(
                "NotebookLM copies %s look like one notebook, but none is shown current; "
                "left alone",
                sorted(nb.id for nb in members),
            )
            continue
        current = shown[0]
        pairs += [
            Duplicate(nb.id, current.id, current.title) for nb in members if nb.id != current.id
        ]
    return sorted(pairs)


class _Child(NamedTuple):
    id: int
    kind: str
    name: str
    cut: bool  # its file name may have been cut short


def _stem(source_id: str) -> str:
    """A child key's file name without its extension."""
    name = source_id.rsplit("/", 1)[-1]
    for suffix in (".html", ".md", ".json"):
        name = name.removesuffix(suffix)
    return name


def _child_name(source_id: str, kind: str = "") -> str:
    """A child's name from its key: the file name without extension or " metadata".

    An artifact's key ends with " metadata", which a cut name may lose part of
    ("Traces meta"); that part goes too, so the rest still begins the full name.
    """
    name = _stem(source_id)
    if kind == "artifact":
        for end in range(len(METADATA), 1, -1):
            if name.endswith(METADATA[:end]):
                name = name[:-end]
                break
    return name.removesuffix(METADATA).strip()


def _children(conn: sqlite3.Connection, parent_id: int) -> list[_Child]:
    """A notebook's imported children."""
    rows = conn.execute(
        "SELECT id, source_id, metadata FROM items "
        "WHERE parent_id = ? AND source_type = 'notebooklm'",
        (parent_id,),
    ).fetchall()
    children = []
    for id_, source_id, raw in rows:
        kind = _metadata(raw).get("type")
        if kind in CHILD_TYPES and source_id:
            key = str(source_id)
            file_name = key.rsplit("/", 1)[-1]
            if kind == "artifact":
                file_name += ".json"  # an artifact's key is its description's name without it
            cut = len(file_name) == CUT_FILE
            children.append(_Child(int(id_), str(kind), _child_name(key, str(kind)), cut))
    return children


def _child_pairs(old: list[_Child], current: list[_Child]) -> list[tuple[int, int]]:
    """(old child, current child) pairs: same kind, and the same name, or a cut name the
    current one begins with. A name matching several is left alone."""
    pairs = []
    taken: set[int] = set()
    for child in old:
        if not child.name:
            continue
        same = [c for c in current if c.kind == child.kind and c.id not in taken]
        matches = [c for c in same if c.name == child.name]
        if not matches and child.cut:
            matches = [c for c in same if c.name.startswith(child.name)]
        if len(matches) == 1:
            pairs.append((child.id, matches[0].id))
            taken.add(matches[0].id)
    return pairs


def _remember_folders(conn: sqlite3.Connection, old_id: int, current_id: int) -> None:
    """Add the old copy's folder, and those it remembered, to the current copy's
    ``metadata.folders``, so a part of its export imported again folds in."""
    rows = dict(
        conn.execute(
            "SELECT id, metadata FROM items WHERE id IN (?, ?)", (old_id, current_id)
        ).fetchall()
    )
    (old_key,) = conn.execute("SELECT source_id FROM items WHERE id = ?", (old_id,)).fetchone()
    old, current = _metadata(rows.get(old_id)), _metadata(rows.get(current_id))
    folders = list(current.get("folders") or [])
    for folder in (old_key, *(old.get("folders") or [])):
        if isinstance(folder, str) and folder not in folders:
            folders.append(folder)
    current["folders"] = folders
    conn.execute("UPDATE items SET metadata = ? WHERE id = ?", (json.dumps(current), current_id))


def dedupe_notebooklm(conn: sqlite3.Connection) -> list[Duplicate]:
    """Merge each older notebook copy, and its copied children, into the current one.

    Returns the notebook pairs merged; safe to run again. The caller commits.
    """
    since = max_history_id(conn)
    pairs = find_duplicates(conn)
    moved: list[int] = []
    for pair in pairs:
        old_children = _children(conn, pair.old_id)
        current_children = _children(conn, pair.current_id)
        for old_child, current_child in _child_pairs(old_children, current_children):
            moved += merge_item(conn, old_child, current_child)
        _remember_folders(conn, pair.old_id, pair.current_id)
        moved += merge_item(conn, pair.old_id, pair.current_id)
    if pairs:
        mark_recent_history(conn, since, "notebooklm dedupe", item_ids=moved)
    return pairs

"""NotebookLM notebooks held twice: one notebook under two folder names.

A notebook's items are keyed by its Takeout folder (``Takeout/NotebookLM/<name>``), the
only key every part of a multi-part export shares. The folder name is not stable:

- Exports before November 2026 cut each folder name to 50 characters, and each file
  name to 51 with its extension; later ones give them in full. The same notebook
  imported from both made two notebook items, each with its own notes, reports, chats
  and artifacts.
- Renaming a notebook renames its folder.

A notebook's metadata JSON holds a stable id, kept as ``metadata.notebook_id`` since
this change. Two notebook items are the same notebook when:

- they share that id;
- or, for those stored before it, they have the same creation time (to the
  microsecond) and title;
- or one's folder name was cut (50 characters) and begins exactly one other's. This
  catches a part of an old export with no metadata JSON, which has no date or title.

The older copy is merged into the current one (see ``merge_item``): its links, tags,
collections and children move, then it is deleted. A moved child that is the copy of
one the notebook already has (same kind and name, or a cut name the other's begins
with) is merged into it the same way. ``dedupe_notebooklm`` runs after every import
that stores NotebookLM items, so a later export's new folder names fold in by
themselves.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Collection
from typing import Any, NamedTuple

from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.stubs import merge_item

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

    @property
    def cut(self) -> bool:
        """Whether its folder name may have been cut short by an older export."""
        folder = self.source_id.removeprefix(FOLDERS)
        return self.source_id.startswith(FOLDERS) and len(folder) == CUT_FOLDER

    def cut_of(self, other: _Notebook) -> bool:
        """Whether this folder is the cut form of ``other``'s. Two known, different
        creation times are two notebooks, such as "<50 characters>" and "<them> (2)"."""
        return (
            self.cut
            and len(other.source_id) > len(self.source_id)
            and other.source_id.startswith(self.source_id)
            and not (self.created_at and other.created_at and self.created_at != other.created_at)
        )


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
            )
        )
    return notebooks


def _rank(nb: _Notebook, others: list[_Notebook], recent: Collection[str]) -> tuple:
    """Higher is more current: not a cut folder of another copy; written by the import
    just run; holding the notebook id; then the newer item."""
    return (
        not any(nb.cut_of(other) for other in others),
        nb.source_id in recent,
        nb.notebook_id is not None,
        nb.id,
    )


def find_duplicates(conn: sqlite3.Connection, recent: Collection[str] = ()) -> list[Duplicate]:
    """Pairs of notebook items that are one notebook, each older copy with the current
    one of its group. ``recent``: the notebook keys the import just run wrote."""
    notebooks = _notebooks(conn)
    groups: dict[tuple[str, ...], list[_Notebook]] = defaultdict(list)
    for nb in notebooks:
        if nb.notebook_id:
            groups[("id", nb.notebook_id)].append(nb)
        if nb.created_at and nb.title:
            groups[("made", nb.created_at, nb.title)].append(nb)
        if nb.cut:
            full = [other for other in notebooks if nb.cut_of(other)]
            if len(full) == 1:
                groups[("cut", nb.source_id)] += [nb, full[0]]

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
        current = max(members, key=lambda nb: _rank(nb, members, recent))
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


def dedupe_notebooklm(conn: sqlite3.Connection, recent: Collection[str] = ()) -> list[Duplicate]:
    """Merge each older notebook copy, and its copied children, into the current one.

    ``recent``: the notebook keys the import just run wrote, which win a tie. Returns
    the notebook pairs merged; safe to run again. The caller commits.
    """
    since = max_history_id(conn)
    pairs = find_duplicates(conn, recent)
    moved: list[int] = []
    for pair in pairs:
        old_children = _children(conn, pair.old_id)
        current_children = _children(conn, pair.current_id)
        for old_child, current_child in _child_pairs(old_children, current_children):
            moved += merge_item(conn, old_child, current_child)
        moved += merge_item(conn, pair.old_id, pair.current_id)
    if pairs:
        mark_recent_history(conn, since, "notebooklm dedupe", item_ids=moved)
    return pairs

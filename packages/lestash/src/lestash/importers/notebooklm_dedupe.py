"""NotebookLM notebooks held twice: one notebook under two folder names.

A notebook's items are keyed by its Takeout folder (``Takeout/NotebookLM/<name>``), the
only key every part of a multi-part export shares. The folder name is not stable:

- Exports before November 2026 cut each folder and file name short (about 50
  characters); later ones give them in full. The same notebook imported from both made
  two notebook items, each with its own notes, reports, chats and artifacts.
- Renaming a notebook renames its folder.

A notebook's metadata JSON holds a stable id, kept as ``metadata.notebook_id`` since
this change. Two notebook items are the same notebook when they share that id, or,
for those stored before it, the same creation time (to the microsecond) and title.

The older copy is merged into the current one (see ``merge_item``): its links, tags,
collections and children move, then it is deleted. A moved child that is the copy of
one the notebook already has (same kind, and a name the other's begins with, as a cut
name does) is merged into it the same way. ``dedupe_notebooklm`` runs after every
import of a Takeout zip, so a later export's new folder names fold in by themselves.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, NamedTuple

from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.stubs import merge_item

CHILD_TYPES = ("note", "report", "chat", "artifact")


class Duplicate(NamedTuple):
    old_id: int  # merged into current_id, then deleted
    current_id: int  # kept
    title: str


class _Notebook(NamedTuple):
    id: int
    source_id: str
    title: str
    created_at: str | None
    fetched_at: str | None
    notebook_id: str | None


def _metadata(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _notebooks(conn: sqlite3.Connection) -> list[_Notebook]:
    rows = conn.execute(
        "SELECT id, source_id, title, created_at, fetched_at, metadata FROM items "
        "WHERE source_type = 'notebooklm' AND parent_id IS NULL AND held = 1"
    ).fetchall()
    notebooks = []
    for id_, source_id, title, created_at, fetched_at, raw in rows:
        metadata = _metadata(raw)
        if metadata.get("type") != "notebook":
            continue
        notebooks.append(
            _Notebook(
                int(id_),
                str(source_id or ""),
                (title or "").strip(),
                str(created_at) if created_at else None,
                str(fetched_at) if fetched_at else None,
                metadata.get("notebook_id") or None,
            )
        )
    return notebooks


def _current(a: _Notebook, b: _Notebook) -> tuple[_Notebook, _Notebook]:
    """(old, current) of two copies of one notebook.

    A cut folder name loses to the full one it begins; otherwise the copy imported last
    wins, as later imports will write to its folder; then the newer item.
    """
    if a.source_id != b.source_id:
        if b.source_id.startswith(a.source_id):
            return a, b
        if a.source_id.startswith(b.source_id):
            return b, a
    if (a.fetched_at or "", a.id) > (b.fetched_at or "", b.id):
        return b, a
    return a, b


def find_duplicates(conn: sqlite3.Connection) -> list[Duplicate]:
    """Pairs of notebook items that are one notebook. An item in several pairs is
    merged into the current copy of the group."""
    groups: dict[tuple[str, ...], list[_Notebook]] = defaultdict(list)
    by_id: dict[int, _Notebook] = {}
    for nb in _notebooks(conn):
        by_id[nb.id] = nb
        if nb.notebook_id:
            groups[("id", nb.notebook_id)].append(nb)
        if nb.created_at and nb.title:
            groups[("made", nb.created_at, nb.title)].append(nb)

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
    for id_ in by_id:
        clusters[root(id_)].append(by_id[id_])

    pairs = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        current = members[0]
        for nb in members[1:]:
            _, current = _current(current, nb)
        pairs += [
            Duplicate(nb.id, current.id, current.title) for nb in members if nb.id != current.id
        ]
    return sorted(pairs)


METADATA = " metadata"


def _child_name(source_id: str, kind: str = "") -> str:
    """A child's name from its key: the file name without extension or " metadata".

    An artifact's key ends with " metadata", which a cut name may lose part of
    ("Traces meta"); that part goes too, so the rest still begins the full name.
    """
    name = source_id.rsplit("/", 1)[-1]
    for suffix in (".html", ".md", ".json"):
        name = name.removesuffix(suffix)
    if kind == "artifact":
        for end in range(len(METADATA), 1, -1):
            if name.endswith(METADATA[:end]):
                name = name[:-end]
                break
    return name.removesuffix(METADATA).strip()


def _children(conn: sqlite3.Connection, parent_id: int) -> list[tuple[int, str, str]]:
    """(id, kind, name) of a notebook's imported children."""
    rows = conn.execute(
        "SELECT id, source_id, metadata FROM items "
        "WHERE parent_id = ? AND source_type = 'notebooklm'",
        (parent_id,),
    ).fetchall()
    children = []
    for id_, source_id, raw in rows:
        kind = _metadata(raw).get("type")
        if kind in CHILD_TYPES and source_id:
            children.append((int(id_), str(kind), _child_name(str(source_id), str(kind))))
    return children


def _child_pairs(
    old: list[tuple[int, str, str]], current: list[tuple[int, str, str]]
) -> list[tuple[int, int]]:
    """(old child, current child) pairs: same kind, and the current name is the old one
    or begins with it. A name matching several is left alone."""
    pairs = []
    taken: set[int] = set()
    for old_id, kind, name in old:
        if not name:
            continue
        same = [c for c in current if c[1] == kind and c[0] not in taken]
        exact = [c for c in same if c[2] == name]
        matches = exact or [c for c in same if c[2].startswith(name)]
        if len(matches) == 1:
            pairs.append((old_id, matches[0][0]))
            taken.add(matches[0][0])
    return pairs


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
        moved += merge_item(conn, pair.old_id, pair.current_id)
    if pairs:
        mark_recent_history(conn, since, "notebooklm dedupe", item_ids=moved)
    return pairs

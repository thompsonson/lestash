"""Suggested themes: groups of related items that cut across sources (#266).

See docs/ux-object-model-design.md, "Themes are not a shelf". A theme is a community in
the related-items graph (#219): every ``related`` link not rejected. Each is named from
the most distinctive words in its items' titles, with no LLM.

- Keeping a theme makes a collection of its items; dismissing one marks it so.
- A later run doesn't suggest a group that mostly matches a kept or dismissed theme.
- Each run replaces the suggestions of the last.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

import networkx as nx

from lestash.core.related import KIND

MIN_ITEMS = 4
MIN_SOURCES = 2
# Louvain's resolution: higher makes more, smaller themes. 1.0 is its default.
RESOLUTION = 1.0
SEED = 1  # the same graph gives the same themes
# A group with this share of its items in a kept or dismissed theme counts as that theme
SAME_THEME = 0.5
NAME_WORDS = 3

# Words of letters, perhaps hyphenated: no numbers, ids or hashes ("b901")
WORD = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z+#]*(?:-[A-Za-z]+)*(?![\w-])")
ADDRESS = re.compile(r"\S*(?:://|www\.|\.(?:com|org|net|gr|io|ai|social)\b)\S*")
# Of an item without a title, its text's start stands in for one
TEXT_CHARS = 120
STOP = frozenset(
    (
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "what",
        "when",
        "where",
        "which",
        "while",
        "into",
        "about",
        "your",
        "you",
        "our",
        "are",
        "was",
        "were",
        "will",
        "can",
        "could",
        "would",
        "should",
        "have",
        "has",
        "had",
        "not",
        "but",
        "all",
        "any",
        "its",
        "how",
        "why",
        "who",
        "more",
        "most",
        "some",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "these",
        "those",
        "just",
        "also",
        "very",
        "new",
        "one",
        "two",
        "via",
        "using",
        "use",
        "part",
        "series",
        "vs",
        "per",
        "out",
        "get",
        "got",
    )
)


@dataclass
class ThemeCounts:
    """What a run found."""

    suggested: int = 0
    skipped: int = 0  # groups matching a kept or dismissed theme


def _graph(conn: sqlite3.Connection) -> nx.Graph:
    graph = nx.Graph()
    for a, b, evidence in conn.execute(
        "SELECT l.from_id, l.to_id, l.evidence FROM item_links l "
        "JOIN items x ON x.id = l.from_id JOIN items y ON y.id = l.to_id "
        "WHERE l.kind = ? AND l.status != 'rejected' "
        "AND x.parent_id IS NULL AND y.parent_id IS NULL AND x.held = 1 AND y.held = 1",
        (KIND,),
    ):
        try:
            weight = float(json.loads(evidence)["score"]) if evidence else 1.0
        except (TypeError, ValueError, KeyError):
            weight = 1.0
        graph.add_edge(a, b, weight=weight)
    return graph


def _words(text: str) -> list[str]:
    """The words of a title worth naming a theme by, as written."""
    words = WORD.findall(ADDRESS.sub(" ", text or ""))
    return [w for w in words if len(w) > 2 and w.lower() not in STOP]


def name_themes(groups: list[list[str]]) -> list[str]:
    """A name for each group of titles: its most distinctive words, against the
    other groups' titles (TF-IDF over items), in order of weight.

    A word counts once per item, and words in two or more of the group's items come
    first. Each is written as its items most often write it ("LLMs").
    """
    shared: list[Counter[str]] = []
    written: list[dict[str, Counter[str]]] = []
    for titles in groups:
        items: Counter[str] = Counter()
        forms: dict[str, Counter[str]] = {}
        for title in titles:
            words = _words(title)
            items.update({w.lower() for w in words})
            for w in words:
                forms.setdefault(w.lower(), Counter())[w] += 1
        shared.append(items)
        written.append(forms)
    df = Counter(w for items in shared for w in items)
    names = []
    for items, forms in zip(shared, written, strict=True):
        weight = {
            w: (n > 1, (1 + math.log(n)) * math.log((1 + len(groups)) / (1 + df[w])))
            for w, n in items.items()
        }
        top = sorted(weight, key=lambda w: (weight[w][0], weight[w][1]), reverse=True)
        words = [forms[w].most_common(1)[0][0] for w in top[:NAME_WORDS]]
        name = ", ".join(words)
        names.append(name[:1].upper() + name[1:] if name else "Untitled theme")
    return names


def _overlap(group: set[int], decided: set[int]) -> float:
    """The share of a group's items that a decided theme holds too."""
    return len(group & decided) / len(group) if group else 0.0


def find_themes(conn: sqlite3.Connection) -> ThemeCounts:
    """Replace the suggested themes with those the related-items graph holds now. The
    caller commits."""
    counts = ThemeCounts()
    graph = _graph(conn)
    groups: list[list[int]] = []
    if graph.number_of_edges():
        communities = nx.community.louvain_communities(
            graph, weight="weight", resolution=RESOLUTION, seed=SEED
        )
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT id, source_type, coalesce(nullif(title, ''), substr(content, 1, ?)) "
                "FROM items WHERE id IN "
                f"({','.join('?' * graph.number_of_nodes())})",
                [TEXT_CHARS, *graph.nodes],
            )
        }
        for community in communities:
            members = sorted(i for i in community if i in rows)
            if len(members) >= MIN_ITEMS and len({rows[i][0] for i in members}) >= MIN_SOURCES:
                groups.append(members)
        titles = [[rows[i][1] or "" for i in members] for members in groups]
    else:
        titles = []

    decided = [
        set(json.loads(r[0]))
        for r in conn.execute("SELECT item_ids FROM themes WHERE status IN ('kept', 'dismissed')")
    ]
    # A theme found again with the same items keeps its row and id, so a list
    # already open can still keep or dismiss it
    before = {
        frozenset(json.loads(r[1])): r[0]
        for r in conn.execute("SELECT id, item_ids FROM themes WHERE status = 'suggested'")
    }
    still: set[int] = set()
    for members, name in zip(groups, name_themes(titles), strict=True):
        if any(_overlap(set(members), d) >= SAME_THEME for d in decided):
            counts.skipped += 1
            continue
        same = before.get(frozenset(members))
        if same is not None:
            conn.execute("UPDATE themes SET name = ? WHERE id = ?", (name, same))
            still.add(same)
        else:
            conn.execute(
                "INSERT INTO themes (name, item_ids, status) VALUES (?, ?, 'suggested')",
                (name, json.dumps(members)),
            )
        counts.suggested += 1
    for theme_id in set(before.values()) - still:
        conn.execute("DELETE FROM themes WHERE id = ?", (theme_id,))
    return counts


def suggested(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The suggested themes, largest first."""
    return conn.execute(
        "SELECT id, name, item_ids, created_at FROM themes WHERE status = 'suggested' "
        "ORDER BY json_array_length(item_ids) DESC, id"
    ).fetchall()


class ThemeGone(LookupError):
    """No such suggested theme: decided already, or replaced by a later run."""


def _suggested_one(conn: sqlite3.Connection, theme_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, item_ids FROM themes WHERE id = ? AND status = 'suggested'",
        (theme_id,),
    ).fetchone()
    if row is None:
        raise ThemeGone(f"No suggested theme {theme_id}")
    return row


def keep(conn: sqlite3.Connection, theme_id: int, name: str | None = None) -> int:
    """Make a collection of a suggested theme's items, in its order; returns its id.
    Items deleted since are left out. The caller commits."""
    from lestash.core.collections import add_item

    row = _suggested_one(conn, theme_id)
    collection_id = conn.execute(
        "INSERT INTO collections (name, description) VALUES (?, ?)",
        (name or row["name"], "Kept from a suggested theme"),
    ).lastrowid
    assert collection_id is not None
    ids = json.loads(row["item_ids"])
    present = {
        r[0]
        for r in conn.execute(f"SELECT id FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
    }
    for item_id in ids:
        if item_id in present:
            add_item(conn, collection_id, item_id)
    conn.execute(
        "UPDATE themes SET status = 'kept', collection_id = ?, decided_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (collection_id, theme_id),
    )
    return collection_id


def dismiss(conn: sqlite3.Connection, theme_id: int) -> None:
    """Never suggest this theme, or one mostly like it, again. The caller commits."""
    _suggested_one(conn, theme_id)
    conn.execute(
        "UPDATE themes SET status = 'dismissed', decided_at = CURRENT_TIMESTAMP WHERE id = ?",
        (theme_id,),
    )

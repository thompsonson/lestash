"""The Library (#264): items on shelves by what they are, with facets for each shelf.

A shelf's items come from ``GET /api/items?kind=<its kinds>``; the facets here count
what the shelf holds by person, topic and year, so the app can offer filters with
counts. Shelves are an exact scheme: each kind sits on one shelf.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lestash_server.deps import get_db

router = APIRouter(prefix="/api/library", tags=["library"])

SHELVES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("books", "Books", ("book",)),
    ("papers", "Papers", ("paper",)),
    ("videos", "Videos", ("video",)),
    ("notebooks", "Notebooks", ("notebook", "project")),
    ("posts", "Posts", ("post",)),
    ("chats", "Chats", ("chat",)),
    ("notes", "Notes", ("note",)),
)
FACET_SIZE = 30


class Shelf(BaseModel):
    shelf: str
    label: str
    kinds: list[str]
    count: int


class PersonFacet(BaseModel):
    id: int
    name: str
    count: int


class NamedFacet(BaseModel):
    name: str
    count: int


class ShelfFacets(BaseModel):
    shelf: str
    people: list[PersonFacet]
    topics: list[NamedFacet]
    years: list[NamedFacet]


def _kinds(shelf: str) -> tuple[str, ...]:
    for name, _label, kinds in SHELVES:
        if name == shelf:
            return kinds
    raise HTTPException(status_code=404, detail=f"No shelf {shelf!r}")


# What a shelf holds: top-level items LeStash holds, of the shelf's kinds
def _on_shelf(kinds: tuple[str, ...]) -> str:
    return f"i.parent_id IS NULL AND i.held = 1 AND i.kind IN ({', '.join('?' for _ in kinds)})"


@router.get("", response_model=list[Shelf])
def shelves():
    """The shelves, each with how many items it holds."""
    every = tuple(k for _s, _l, kinds in SHELVES for k in kinds)
    with get_db() as conn:
        counts = dict(
            conn.execute(
                f"SELECT i.kind, count(*) FROM items i WHERE {_on_shelf(every)} GROUP BY i.kind",
                every,
            ).fetchall()
        )
    return [
        Shelf(
            shelf=name,
            label=label,
            kinds=list(kinds),
            count=sum(counts.get(k, 0) for k in kinds),
        )
        for name, label, kinds in SHELVES
    ]


@router.get("/{shelf}/facets", response_model=ShelfFacets)
def facets(shelf: str):
    """A shelf's items counted by person (in any role), topic and year: the counts
    match the shelf's list under the same filter."""
    kinds = _kinds(shelf)
    where = _on_shelf(kinds)
    with get_db() as conn:
        people = [
            PersonFacet(id=r[0], name=r[1], count=r[2])
            for r in conn.execute(
                f"""SELECT p.id, p.name, count(DISTINCT i.id) n FROM items i
                JOIN item_agents a ON a.item_id = i.id JOIN people p ON p.id = a.person_id
                WHERE {where}
                GROUP BY p.id ORDER BY n DESC, p.name LIMIT ?""",
                [*kinds, FACET_SIZE],
            )
        ]
        topics = [
            NamedFacet(name=r[0], count=r[1])
            for r in conn.execute(
                f"""SELECT t.name, count(*) n FROM items i
                JOIN item_tags it ON it.item_id = i.id JOIN tags t ON t.id = it.tag_id
                WHERE {where} GROUP BY t.name ORDER BY n DESC, t.name LIMIT ?""",
                [*kinds, FACET_SIZE],
            )
        ]
        years = [
            NamedFacet(name=r[0], count=r[1])
            for r in conn.execute(
                f"""SELECT strftime('%Y', i.created_at) y, count(*) n FROM items i
                WHERE {where} AND i.created_at IS NOT NULL
                GROUP BY y HAVING y IS NOT NULL ORDER BY y DESC""",
                kinds,
            )
        ]
    return ShelfFacets(shelf=shelf, people=people, topics=topics, years=years)

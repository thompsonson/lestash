"""People, channels and AIs (#263): a list by kind, and a page each.

A channel is a ``people`` row of kind ``organization``; an AI is ``software``. Their
items come from ``GET /api/items?person=<id>`` (and ``&role=``).
"""

from fastapi import APIRouter, HTTPException, Query
from lestash.core.kinds import KINDS
from pydantic import BaseModel

from lestash_server.deps import get_db

router = APIRouter(prefix="/api/people", tags=["people"])

# What the People page's switch calls each kind
SWITCH = {"people": ("person", "group"), "channels": ("organization",), "ai": ("software",)}


class Account(BaseModel):
    platform: str
    identifier: str
    url: str | None = None
    display_name: str | None = None


class PersonSummary(BaseModel):
    id: int
    name: str
    kind: str
    is_self: bool = False
    items: int = 0  # items they made, voiced, published or received
    platforms: list[str] = []


class PersonListResponse(BaseModel):
    people: list[PersonSummary]
    total: int
    limit: int
    offset: int


class RoleCount(BaseModel):
    role: str
    kind: str
    kind_label: str
    count: int


class VerbCount(BaseModel):
    verb: str
    count: int
    last_at: str | None = None


class PersonDetail(PersonSummary):
    accounts: list[Account] = []
    made: list[RoleCount] = []  # their items by role and kind
    mentioned_in: int = 0
    your_activity: list[VerbCount] = []  # what you did to their items


def _summary(row) -> PersonSummary:
    return PersonSummary(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        is_self=bool(row["is_self"]),
        items=row["items"] or 0,
        platforms=sorted(set((row["platforms"] or "").split(","))) if row["platforms"] else [],
    )


_COUNTS = """
    (SELECT count(DISTINCT a.item_id) FROM item_agents a WHERE a.person_id = p.id) AS items,
    (SELECT group_concat(DISTINCT i.platform) FROM person_identities i
     WHERE i.person_id = p.id) AS platforms
"""


@router.get("", response_model=PersonListResponse)
def list_people(
    switch: str = Query("people", description="people, channels or ai"),
    q: str | None = Query(None, description="Names containing this"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """People of one kind, those with the most items first."""
    kinds = SWITCH.get(switch)
    if kinds is None:
        raise HTTPException(status_code=422, detail=f"switch: one of {', '.join(SWITCH)}")
    where = [f"p.kind IN ({', '.join('?' for _ in kinds)})"]
    params: list = list(kinds)
    if q and q.strip():
        where.append("p.name LIKE ? ESCAPE '\\'")
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    body = "FROM people p WHERE " + " AND ".join(where)
    with get_db() as conn:
        total = conn.execute(f"SELECT count(*) {body}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT p.id, p.name, p.kind, p.is_self, {_COUNTS} {body}"
            " ORDER BY p.is_self DESC, items DESC, p.name LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return PersonListResponse(
        people=[_summary(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/me", response_model=PersonDetail)
def me():
    """You."""
    with get_db() as conn:
        row = conn.execute("SELECT id FROM people WHERE is_self = 1").fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No person is marked as you")
    return person(row[0])


@router.get("/{person_id}", response_model=PersonDetail)
def person(person_id: int):
    """One person, channel or AI: accounts, what they made, your activity with them."""
    with get_db() as conn:
        row = conn.execute(
            f"SELECT p.id, p.name, p.kind, p.is_self, {_COUNTS} FROM people p WHERE p.id = ?",
            (person_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Person not found")
        accounts = [
            Account(
                platform=r["platform"],
                identifier=r["identifier"],
                url=r["url"],
                display_name=r["display_name"],
            )
            for r in conn.execute(
                "SELECT platform, identifier, url, display_name FROM person_identities"
                " WHERE person_id = ? ORDER BY platform, identifier",
                (person_id,),
            )
        ]
        made = [
            RoleCount(
                role=r["role"],
                kind=r["kind"] or "document",
                kind_label=KINDS[r["kind"]].label if r["kind"] in KINDS else (r["kind"] or ""),
                count=r["n"],
            )
            for r in conn.execute(
                "SELECT a.role, i.kind, count(*) n FROM item_agents a JOIN items i"
                " ON i.id = a.item_id WHERE a.person_id = ? GROUP BY a.role, i.kind"
                " ORDER BY n DESC",
                (person_id,),
            )
        ]
        mentioned = conn.execute(
            "SELECT count(*) FROM item_mentions WHERE person_id = ?", (person_id,)
        ).fetchone()[0]
        # Your own activity on their items; a person who is you has none "with you"
        activity = []
        if not row["is_self"]:
            activity = [
                VerbCount(verb=r["verb"], count=r["n"], last_at=r["last_at"])
                for r in conn.execute(
                    # IN, not a join: a person with two roles on an item counts once
                    "SELECT act.verb, count(*) n, max(act.occurred_at) last_at"
                    " FROM activities act WHERE act.actor_id IS NULL AND act.item_id IN"
                    " (SELECT item_id FROM item_agents WHERE person_id = ?)"
                    " GROUP BY act.verb ORDER BY n DESC",
                    (person_id,),
                )
            ]
    summary = _summary(row)
    return PersonDetail(
        **summary.model_dump(),
        accounts=accounts,
        made=made,
        mentioned_in=mentioned,
        your_activity=activity,
    )

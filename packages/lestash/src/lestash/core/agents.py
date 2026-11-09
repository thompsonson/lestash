"""Agents: who made, voiced, published or received an item.

See docs/content-and-activity-ontology-design.md, "The people ontology". After each
sync, this pass reads the identifiers importers already store and records:

- an account (``person_identities``) per platform identifier: a YouTube channel id, a
  LinkedIn member URN, a Bluesky DID, an email address, a mailing list, a chat role;
- a person (``people``) per new account. Nothing joins two accounts into one person
  automatically, except yours: an account the source marks as yours belongs to the
  one ``is_self`` person. Your accounts are recorded first, so an address of yours
  first seen on an email you received is still yours;
- a person with no account for an agent known only by name, such as an arXiv or
  Audible author: one per item, as one name can be several people. The name linker
  will suggest which are the same;
- an ``item_agents`` row per item and role: ``author``, ``creator`` (a channel's
  video), ``narrator``, ``publisher`` or ``recipient``.

An importer's identifier is exact, so each account it gives is ``confirmed``. Sources
that hold only free text, such as a Kobo book's author line, wait: splitting
"RR Haywood, Joe Jameson" into people would be a guess.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple

YOU = "You"  # your name until a source gives it
NAME = "name"  # the evidence of an agent known by name only, with no account
# Not a role: the item names them without their taking part, so it is an item_mentions
# row, not an item_agents one
MENTIONED = "mentioned"
# Platforms whose refs give one name per account in a run (the best known), so a
# better one found later can replace it
CANONICAL_NAMES = frozenset({"linkedin"})
LINKEDIN_ENTITIES = {
    "com.linkedin.common.MemberAttributedEntity": ("member", "person"),
    "com.linkedin.common.CompanyAttributedEntity": ("company", "organization"),
}
LIST_ID = re.compile(r"<([^<>]+)>")

# Chat sources: the author word (or Claude's metadata.sender) -> the software agent that
# wrote the turn, or None for your own turns
CHAT_AUTHORS: dict[str, dict[str, str | None]] = {
    "gemini": {"user": None, "model": "Gemini"},
    "mistral": {"user": None, "assistant": "Mistral"},
    "claude": {"human": None, "assistant": "Claude"},
    "claude-code": {"user": None},
}


class AgentRef(NamedTuple):
    """One agent of an item, as its source names it."""

    role: str  # author, creator, narrator, publisher, recipient; or MENTIONED
    platform: str
    identifier: str | None  # None: known by name only, so no account
    name: str
    kind: str  # person, organization, group, software
    url: str | None = None
    position: int | None = None
    is_self: bool = False
    evidence: str | None = None


class _Row(NamedTuple):
    id: int
    source_type: str
    author: str | None
    is_own: bool
    metadata: dict[str, Any]


class _Context(NamedTuple):
    """What the extractors need beyond the row."""

    linkedin_self: frozenset[str]  # your LinkedIn member URNs (changelog owner)
    linkedin_names: dict[str, str]  # URN -> name, as mentions write it
    your_name: str | None  # as stored, or as your profile gives it; None if unknown


def _names(value: Any) -> list[str]:
    """A metadata list of names, without blanks; [] if it isn't a list."""
    if not isinstance(value, list):
        return []
    return [n.strip() for n in value if isinstance(n, str) and n.strip()]


def _youtube(row: _Row, _: _Context) -> Iterator[AgentRef]:
    meta = row.metadata
    url = meta.get("channel_url")
    channel_id = meta.get("channel_id")
    if not channel_id and isinstance(url, str) and "/channel/" in url:
        channel_id = url.rsplit("/channel/", 1)[1].strip("/") or None
    name = meta.get("channel_title") or row.author
    if not isinstance(channel_id, str) or not channel_id or not name:
        return  # a name alone could be any of several channels
    yield AgentRef(
        "creator",
        "youtube",
        channel_id,
        str(name),
        "organization",
        url=url if isinstance(url, str) else f"https://www.youtube.com/channel/{channel_id}",
        evidence="channel id",
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def linkedin_mentions(metadata: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """(URN, kind, name as written) for each member or company a LinkedIn post, comment
    or message tags. Its attributes give the URN and where the name sits in the text,
    counted in UTF-16 code units, as an emoji before it shows."""
    activity = _dict(_dict(metadata.get("raw")).get("activity"))
    message = _dict(activity.get("message"))
    share = _dict(
        _dict(_dict(activity.get("specificContent")).get("com.linkedin.ugc.ShareContent")).get(
            "shareCommentary"
        )
    )
    # The offsets count in the text beside them, so both come from one object
    holder = message if message.get("text") else share
    text, attributes = holder.get("text"), holder.get("attributes")
    if not isinstance(text, str) or not isinstance(attributes, list):
        return
    units = text.encode("utf-16-le")
    for attribute in attributes:
        attribute = _dict(attribute)
        start, length = attribute.get("start"), attribute.get("length")
        if not isinstance(start, int) or not isinstance(length, int):
            continue
        if start < 0 or length <= 0 or (start + length) * 2 > len(units):
            continue
        name = units[start * 2 : (start + length) * 2].decode("utf-16-le", "replace")
        if "\ufffd" in name:
            continue  # the span splits an emoji: the offsets don't fit this text
        for entity, (field, kind) in LINKEDIN_ENTITIES.items():
            urn = _dict(_dict(attribute.get("value")).get(entity)).get(field)
            if isinstance(urn, str):
                yield urn, kind, " ".join(name.split())


def _linkedin(row: _Row, ctx: _Context) -> Iterator[AgentRef]:
    """The author, a member (urn:li:person) or a company page (urn:li:company), and the
    members and companies the text tags. The API gives no names: one comes from how a
    mention writes it, else the URN stands in. A name you set is kept on the person."""
    seen: set[str] = set()
    for urn, kind, _ in linkedin_mentions(row.metadata):
        if urn in seen:
            continue
        seen.add(urn)
        yield AgentRef(
            MENTIONED,
            "linkedin",
            urn,
            ctx.linkedin_names.get(urn, urn),
            kind,
            is_self=urn in ctx.linkedin_self,
            evidence="tagged",
        )
    author = row.author
    if not author:
        return
    urn = author
    if urn.startswith("urn:li:person:"):
        kind, evidence = "person", "member URN"
    elif urn.startswith("urn:li:company:"):
        kind, evidence = "organization", "company URN"
    else:
        return
    yield AgentRef(
        "author",
        "linkedin",
        urn,
        ctx.linkedin_names.get(urn, urn),
        kind,
        is_self=urn in ctx.linkedin_self,
        evidence=evidence,
    )


def _bluesky(row: _Row, _: _Context) -> Iterator[AgentRef]:
    meta = row.metadata
    did, handle = meta.get("author_did"), meta.get("author_handle")
    if not isinstance(did, str) or not did:
        return
    yield AgentRef(
        "author",
        "bluesky",
        did,
        str(meta.get("author_display_name") or handle or row.author or did),
        "person",
        url=f"https://bsky.app/profile/{handle}" if handle else None,
        is_self=row.is_own,
        evidence="DID",
    )


def _arxiv(row: _Row, _: _Context) -> Iterator[AgentRef]:
    for position, name in enumerate(_names(row.metadata.get("authors")), start=1):
        yield AgentRef("author", "arxiv", None, name, "person", position=position, evidence=NAME)


def _audible(row: _Row, _: _Context) -> Iterator[AgentRef]:
    meta = row.metadata
    for role, key in (("author", "authors"), ("narrator", "narrators")):
        for position, name in enumerate(_names(meta.get(key)), start=1):
            yield AgentRef(role, "audible", None, name, "person", position=position, evidence=NAME)
    publisher = meta.get("publisher")
    if isinstance(publisher, str) and publisher.strip():
        yield AgentRef(
            "publisher", "audible", None, publisher.strip(), "organization", evidence=NAME
        )


def _addresses(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [v.strip().lower() for v in values if isinstance(v, str) and "@" in v]


def _your_email(row: _Row, ctx: _Context) -> bool:
    """Whether you sent an email: it is marked yours and sent under your name. Until
    your name is known, none is. An import that leaves the mark out marks every message yours, and a
    sender's address put on you would take their messages with it."""
    return row.is_own and ctx.your_name is not None and row.author == ctx.your_name


def _email(row: _Row, ctx: _Context) -> Iterator[AgentRef]:
    """The sender and recipients. A message a mailing list relays ("'Ann' via UAI
    Mailing List") comes from the list's address, not Ann's: the list is its
    publisher, a group, and the poster is not known by any account."""
    meta = row.metadata
    list_id = meta.get("list_id")
    # 'List-Id: "Python-Dev" <python-dev.python.org>': the id is between < and >; the
    # description before it can change
    match = LIST_ID.search(list_id) if isinstance(list_id, str) else None
    list_key = (match.group(1) if match else str(list_id or "")).strip().lower()
    relayed = bool(list_key) and " via " in (row.author or "")
    if list_key:
        name = row.author.rsplit(" via ", 1)[1].strip() if relayed and row.author else list_key
        yield AgentRef("publisher", "mailing-list", list_key, name, "group", evidence="List-Id")
    for address in [] if relayed else _addresses(meta.get("from_email"))[:1]:
        yield AgentRef(
            "author",
            "email",
            address,
            row.author or address,
            "person",
            url=f"mailto:{address}",
            is_self=_your_email(row, ctx),
            evidence="address",
        )
    to_name = meta.get("to_name") if isinstance(meta.get("to_name"), str) else None
    recipients = _addresses(meta.get("to_email"))
    for address in recipients:
        name = to_name if to_name and len(recipients) == 1 else address
        yield AgentRef(
            "recipient",
            "email",
            address,
            name,
            "person",
            url=f"mailto:{address}",
            evidence="address",
        )


def _chat(row: _Row, _: _Context) -> Iterator[AgentRef]:
    roles = CHAT_AUTHORS[row.source_type]
    word = row.metadata.get("sender") if row.source_type == "claude" else row.author
    if not isinstance(word, str) or word not in roles:
        return
    software = roles[word]
    if software is None:
        yield AgentRef("author", row.source_type, word, YOU, "person", is_self=True)
    else:
        yield AgentRef("author", row.source_type, word, software, "software")


EXTRACTORS: dict[str, Callable[[_Row, _Context], Iterator[AgentRef]]] = {
    "youtube": _youtube,
    "linkedin": _linkedin,
    "bluesky": _bluesky,
    "arxiv": _arxiv,
    "audible": _audible,
    "email": _email,
    "gmail": _email,
    **dict.fromkeys(CHAT_AUTHORS, _chat),
}


def _context(conn: sqlite3.Connection) -> _Context:
    owners = frozenset(
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT json_extract(metadata, '$.raw.owner') FROM items "
            "WHERE source_type = 'linkedin' AND json_valid(metadata) "
            "AND json_extract(metadata, '$.raw.owner') LIKE 'urn:li:person:%'"
        )
    )
    # A name as mentions write it: the longest, as "Matthew Thompson" over "Matthew"
    names: dict[str, str] = {}
    for (metadata_json,) in conn.execute(
        "SELECT metadata FROM items WHERE source_type = 'linkedin' AND held = 1 "
        "AND metadata LIKE '%AttributedEntity%'"
    ):
        try:
            metadata = _dict(json.loads(metadata_json))
        except (TypeError, ValueError):
            continue
        for urn, _, name in linkedin_mentions(metadata):
            if name and len(name) > len(names.get(urn, "")):
                names[urn] = name
    stored = conn.execute(
        "SELECT name FROM people WHERE is_self = 1 AND name != ?", (YOU,)
    ).fetchone()
    return _Context(owners, names, stored[0] if stored else _your_name(conn, owners))


def agents_of(
    conn: sqlite3.Connection, ctx: _Context | None = None
) -> Iterator[tuple[int, str, AgentRef]]:
    """(item id, source type, agent) for every held item whose source names its agents."""
    ctx = ctx or _context(conn)
    marks = ",".join("?" * len(EXTRACTORS))
    rows = conn.execute(
        f"SELECT id, source_type, author, is_own_content, metadata FROM items "
        f"WHERE held = 1 AND source_type IN ({marks})",
        list(EXTRACTORS),
    )
    for item_id, source_type, author, is_own, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        row = _Row(item_id, source_type, author, bool(is_own), metadata)
        for ref in EXTRACTORS[source_type](row, ctx):
            yield item_id, source_type, ref


class _People:
    """Accounts and people, made on first sight and remembered for the run."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # An account not confirmed (suggested, rejected) is kept but gives no agent
        self.accounts: dict[tuple[str, str], int | None] = {}
        self.display: dict[tuple[str, str], str | None] = {}
        for platform, identifier, person_id, display in conn.execute(
            "SELECT platform, identifier, "
            "CASE WHEN status = 'confirmed' THEN person_id END, display_name "
            "FROM person_identities"
        ):
            self.accounts[(platform, identifier)] = person_id
            self.display[(platform, identifier)] = display
        # People named by an account's identifier, waiting for a source to name them
        self.unnamed: set[int] = {
            person_id
            for (person_id,) in conn.execute(
                "SELECT p.id FROM people p JOIN person_identities i ON i.person_id = p.id "
                "WHERE p.name = i.identifier AND p.is_self = 0"
            )
        }
        self._self_id: int | None = None

    def you(self, name: str | None) -> int:
        """The is_self person, made with this name if there is none yet."""
        if self._self_id is None:
            row = self.conn.execute("SELECT id FROM people WHERE is_self = 1").fetchone()
            if row is None:
                row = self.conn.execute(
                    "INSERT INTO people (name, kind, is_self) VALUES (?, 'person', 1) RETURNING id",
                    (name or YOU,),
                ).fetchone()
            self._self_id = int(row[0])
        return self._self_id

    def person(self, ref: AgentRef, origin: str) -> int | None:
        """The person an account belongs to, made with the account on first sight; None
        when the account is kept but belongs to no one (its person was deleted)."""
        if ref.identifier is None:
            return self.named(ref)
        key = (ref.platform, ref.identifier)
        if key in self.accounts:
            person_id = self.accounts[key]
            if ref.is_self and person_id is not None and person_id != self.you(None):
                person_id = self.claim(key, person_id)
                if person_id == self.you(None):
                    self.name_you(ref)
            if ref.platform in CANONICAL_NAMES and person_id not in (None, self.you(None)):
                self.rename(key, person_id, ref.name)
            if person_id in self.unnamed and ref.name != ref.identifier:
                # Named by its identifier until a source gave a name, as a list's
                # address is until a relayed message names the list
                self.conn.execute(
                    "UPDATE people SET name = ? WHERE id = ? AND name = ?",
                    (ref.name, person_id, ref.identifier),
                )
                self.conn.execute(
                    "UPDATE person_identities SET display_name = ? "
                    "WHERE platform = ? AND identifier = ? AND display_name = identifier",
                    (ref.name, *key),
                )
                self.unnamed.discard(person_id)
            return person_id
        if ref.is_self:
            person_id = self.you(None)
            self.name_you(ref)
        else:
            person_id = self.conn.execute(
                "INSERT INTO people (name, kind) VALUES (?, ?) RETURNING id",
                (ref.name, ref.kind),
            ).fetchone()[0]
            if ref.name == ref.identifier:
                self.unnamed.add(person_id)
        self.conn.execute(
            "INSERT INTO person_identities (person_id, platform, identifier, display_name, "
            "url, status, evidence, origin) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (person_id, ref.platform, ref.identifier, ref.name, ref.url, ref.evidence, origin),
        )
        self.accounts[key] = person_id
        return person_id

    def rename(self, key: tuple[str, str], person_id: int | None, name: str) -> None:
        """Give a person the account's better name, while they still have the name the
        pass gave them: a name you set (people.name_origin) is kept."""
        old = self.display.get(key)
        if name in (old, key[1]):
            return
        self.conn.execute(
            "UPDATE people SET name = ? WHERE id = ? AND name = ? AND name_origin IS NULL",
            (name, person_id, old),
        )
        self.conn.execute(
            "UPDATE person_identities SET display_name = ? WHERE platform = ? AND identifier = ?",
            (name, *key),
        )
        self.display[key] = name
        self.unnamed.discard(person_id)  # type: ignore[arg-type]

    def name_you(self, ref: AgentRef) -> None:
        """The first account of yours that gives a name, not an identifier, names you."""
        if ref.name not in (YOU, ref.identifier):
            self.conn.execute(
                "UPDATE people SET name = ? WHERE id = ? AND name = ?",
                (ref.name, self.you(None), YOU),
            )

    def claim(self, key: tuple[str, str], person_id: int) -> int:
        """An account of yours that an earlier pass gave someone else, as an address of
        yours first seen on an email you received. It moves to you when it was made by
        an import and is that person's only account; else it stays, as someone chose."""
        others = self.conn.execute(
            "SELECT COUNT(*), MIN(origin LIKE 'import:%') FROM person_identities "
            "WHERE person_id = ?",
            (person_id,),
        ).fetchone()
        if others[0] != 1 or not others[1]:
            return person_id
        you = self.you(None)
        self.conn.execute(
            "UPDATE person_identities SET person_id = ? WHERE platform = ? AND identifier = ?",
            (you, *key),
        )
        self.conn.execute(
            "UPDATE OR IGNORE item_agents SET person_id = ? WHERE person_id = ?",
            (you, person_id),
        )
        # Null is you, as for every activity of yours
        self.conn.execute("UPDATE activities SET actor_id = NULL WHERE actor_id = ?", (person_id,))
        self.conn.execute(
            "UPDATE OR IGNORE item_mentions SET person_id = ? WHERE person_id = ?",
            (you, person_id),
        )
        self.conn.execute("DELETE FROM people WHERE id = ? AND is_self = 0", (person_id,))
        self.accounts[key] = you
        self.unnamed.discard(person_id)
        return you

    def named(self, ref: AgentRef) -> int:
        """A new person known only by name, with no account."""
        return int(
            self.conn.execute(
                "INSERT INTO people (name, kind) VALUES (?, ?) RETURNING id",
                (ref.name, ref.kind),
            ).fetchone()[0]
        )


def _your_name(conn: sqlite3.Connection, ctx_self: frozenset[str]) -> str | None:
    """Your name as your own LinkedIn account or Bluesky posts give it, if they do."""
    for urn in sorted(ctx_self):
        row = conn.execute(
            "SELECT display_name FROM person_identities WHERE platform = 'linkedin' "
            "AND identifier = ? AND display_name NOT IN ('', identifier)",
            (urn,),
        ).fetchone()
        if row:
            return str(row[0])
    row = conn.execute(
        "SELECT json_extract(metadata, '$.author_display_name') FROM items "
        "WHERE source_type = 'bluesky' AND is_own_content = 1 AND json_valid(metadata) "
        "AND json_extract(metadata, '$.author_display_name') != '' LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def link_agents(conn: sqlite3.Connection) -> int:
    """Record the agents of every item whose source names them, and the people it
    mentions. Returns how many item agents and mentions were added; safe to run again.
    The caller commits."""
    if not conn.in_transaction:
        # Write-lock first: two passes at once (a sync, an upload) would each make the
        # people known by name that the other is making
        conn.execute("BEGIN IMMEDIATE")
    ctx = _context(conn)
    people = _People(conn)
    people.you(ctx.your_name)
    # An agent known by name only has no account to find it by: its item, role and
    # position say it is already recorded, even once the name linker gives it one
    named = {
        (item_id, role, position)
        for item_id, role, position in conn.execute(
            "SELECT item_id, role, position FROM item_agents WHERE evidence = ?", (NAME,)
        )
    }
    # Yours first: an account of yours must not be made someone else's because an item
    # naming it as a recipient came first
    found = sorted(agents_of(conn, ctx), key=lambda found: not found[2].is_self)
    rows, mentions = [], []
    for item_id, source_type, ref in found:
        if ref.identifier is None and (item_id, ref.role, ref.position) in named:
            continue
        origin = f"import:{source_type}"
        person_id = people.person(ref, origin)
        if person_id is None:
            continue
        if ref.role == MENTIONED:
            mentions.append((item_id, person_id, ref.evidence, origin))
        else:
            rows.append((item_id, person_id, ref.role, ref.position, ref.evidence, origin))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO item_agents (item_id, person_id, role, position, evidence, "
        "origin) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    # An importer's tag names one member for certain: confirmed, as its accounts are
    conn.executemany(
        "INSERT OR IGNORE INTO item_mentions (item_id, person_id, status, evidence, origin) "
        "VALUES (?, ?, 'confirmed', ?, ?)",
        mentions,
    )
    return conn.total_changes - before


def agents_for(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    """An item's agents, by role and position, then the people it mentions (role
    ``mentioned``, confirmed only), each with the person's accounts."""
    rows = conn.execute(
        """
        SELECT role, position, id, name, kind, is_self FROM (
            SELECT a.role, a.position, p.id, p.name, p.kind, p.is_self, 0 AS mention
            FROM item_agents a JOIN people p ON p.id = a.person_id
            WHERE a.item_id = :item
            UNION ALL
            SELECT :mentioned, NULL, p.id, p.name, p.kind, p.is_self, 1
            FROM item_mentions m JOIN people p ON p.id = m.person_id
            WHERE m.item_id = :item AND m.status = 'confirmed'
        )
        ORDER BY mention, role, position IS NULL, position, name
        """,
        {"item": item_id, "mentioned": MENTIONED},
    ).fetchall()
    agents = []
    for role, position, person_id, name, kind, is_self in rows:
        accounts = [
            {"platform": r[0], "identifier": r[1], "url": r[2]}
            for r in conn.execute(
                "SELECT platform, identifier, url FROM person_identities "
                "WHERE person_id = ? AND status = 'confirmed' ORDER BY platform, id",
                (person_id,),
            )
        ]
        agents.append(
            {
                "role": role,
                "position": position,
                "person": {"id": person_id, "name": name, "kind": kind, "is_self": bool(is_self)},
                "accounts": accounts,
            }
        )
    return agents

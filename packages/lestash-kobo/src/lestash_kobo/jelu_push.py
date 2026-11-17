"""Kobo reading status into Jelu: ``lestash kobo push-jelu`` (#203).

Jelu is the tracker of record for all reading, ebooks included. The Kobo knows which
books are being read and which are finished; this sends that to Jelu as reading events.

- Only titles the Kobo shows as reading or finished, and only books: a title with an
  ISBN, one already in Jelu, or one asked for by item id.
- A Kobo book matches a Jelu book by ISBN, else by title with an author surname in
  common. A title match without a shared author is weak: listed, never acted on.
- A status is only ever moved forward (nothing, reading, finished). Nothing in Jelu is
  updated in place, moved back or deleted. Progress (percent read) is not sent: Jelu's
  update call clears the fields it isn't given.
- A match becomes an ``alternate_of`` link in LeStash once both items exist. A rejected
  link means "not the same book", and the pair is never matched again.
- A rerun changes nothing: Jelu then shows each status already.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lestash_kobo.reader import SOURCE_TYPE, parse_date, title_kind

LOG_SOURCE = "kobo-jelu"
LINK_ORIGIN = "linker:jelu"
JELU_BOOK_PREFIX = "jelu:book:"

READING, FINISHED = "CURRENTLY_READING", "FINISHED"
KOBO_STATUS = {1: "reading", 2: "finished"}  # ReadStatus; 0 is unread

# What a plan entry does
START, FINISH = "start", "finish"  # an event on a book Jelu has
CREATE = "create"  # a new Jelu book, then its event
NOTHING = "in step"  # Jelu shows the status already
CONFLICT = "conflict"  # Jelu says something the Kobo can't move forward
WEAK = "weak match"  # a title match without a shared author
LEFT_OUT = "left out"  # not a book: no ISBN, not in Jelu, not asked for
REJECTED = "rejected"  # you rejected the link: not the same book
SAME_BOOK = "same book"  # another Kobo title is the same book, and sends its status


@dataclass
class KoboTitle:
    content_id: str
    title: str
    authors: list[str]
    isbn13: str | None
    status: str  # reading, finished
    last_read: datetime | None
    kind: str
    item_id: int | None = None  # its LeStash item, once imported


@dataclass
class JeluBook:
    book_id: str
    title: str
    authors: list[str]
    isbns: set[str]
    userbook_id: str | None = None  # None: in Jelu, but not in your library
    last_event: str | None = None  # CURRENTLY_READING, FINISHED, DROPPED
    last_event_at: datetime | None = None


@dataclass
class Step:
    action: str
    kobo: KoboTitle
    jelu: JeluBook | None = None
    how: str | None = None  # isbn, title and author, title only
    event: str | None = None  # the reading event to send
    when: datetime | None = None  # its date

    def text(self) -> str:
        name = f"{self.kobo.title}" + (
            f" by {', '.join(self.kobo.authors)}" if self.kobo.authors else ""
        )
        dated = self.when or self.kobo.last_read
        when = f", {dated.date()}" if dated else ""
        jelu = f" ↔ Jelu “{self.jelu.title}”" if self.jelu else ""
        how = f" ({self.how})" if self.how else ""
        if self.action in (START, FINISH, CREATE):
            verb = "reading" if self.event == READING else "finished"
            add = "create in Jelu, then " if self.action == CREATE else ""
            return f"{add}mark {verb}{when}: {name}{jelu}{how}"
        if self.action == CONFLICT:
            says = self.jelu.last_event if self.jelu else "?"
            return f"Kobo says {self.kobo.status}, Jelu says {says}: {name}{jelu}"
        return f"{name}{jelu}{how}"


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    def of(self, *actions: str) -> list[Step]:
        return [s for s in self.steps if s.action in actions]

    @property
    def changes(self) -> list[Step]:
        return self.of(START, FINISH, CREATE)


# --- the Kobo side ---


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip('"“”').strip()


def split_authors(attribution: str) -> list[str]:
    """``"Ann Lee, Bo Chen"`` or ``Ann Lee & Bo Chen`` → each name."""
    names = re.split(r",|&|\band\b|;", _clean(attribution))
    return [n.strip() for n in names if n.strip()]


def isbn13(value: Any) -> str | None:
    """An ISBN as 13 digits: ``urn:isbn:``, dashes and spaces dropped; ISBN-10 converted."""
    raw = re.sub(r"[^0-9Xx]", "", re.sub(r"(?i)^urn:isbn:", "", str(value or "").strip()))
    if len(raw) == 10:
        body = "978" + raw[:9]
    elif len(raw) == 13 and raw.isdigit():
        return raw if _check13(raw[:12]) == raw[12] else None
    else:
        return None
    if not body.isdigit():
        return None
    return body + _check13(body)


def _check13(first12: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def kobo_titles(rows: Iterable[dict[str, Any]]) -> list[KoboTitle]:
    """The titles the Kobo shows as reading or finished; notebooks never count."""
    titles = []
    for row in rows:
        try:
            status = KOBO_STATUS.get(int(row.get("ReadStatus") or 0))
        except (TypeError, ValueError):
            status = None
        kind = title_kind(row)
        if not status or kind == "notebook":
            continue
        titles.append(
            KoboTitle(
                content_id=row["ContentID"],
                title=_clean(row.get("Title")) or row["ContentID"],
                authors=split_authors(row.get("Attribution") or ""),
                isbn13=isbn13(row.get("ISBN")),
                status=status,
                last_read=parse_date(row.get("DateLastRead")),
                kind=kind,
            )
        )
    return titles


# --- the Jelu side ---


def _names(entries: Any) -> list[str]:
    return [e["name"] for e in entries or [] if isinstance(e, dict) and e.get("name")]


def _book(book: dict[str, Any]) -> JeluBook:
    isbns = {i for i in (isbn13(book.get("isbn13")), isbn13(book.get("isbn10"))) if i}
    return JeluBook(
        book_id=str(book["id"]),
        title=_clean(book.get("title")),
        authors=_names(book.get("authors")),
        isbns=isbns,
    )


def jelu_books(
    userbooks: Iterable[dict[str, Any]], books: Iterable[dict[str, Any]]
) -> list[JeluBook]:
    """Your library, then the books Jelu holds outside it (matched, never duplicated)."""
    found: dict[str, JeluBook] = {}
    for userbook in userbooks:
        book = userbook.get("book") or {}
        if not book.get("id"):
            continue
        entry = _book(book)
        entry.userbook_id = str(userbook.get("id")) if userbook.get("id") else None
        entry.last_event = userbook.get("lastReadingEvent")
        entry.last_event_at = _jelu_time(userbook.get("lastReadingEventDate"))
        found[entry.book_id] = entry
    for book in books:
        if book.get("id") and str(book["id"]) not in found:
            found[str(book["id"])] = _book(book)
    return list(found.values())


def _jelu_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def event_time(title: KoboTitle, book: JeluBook | None, event: str) -> datetime | None:
    """The Kobo's last read, but always after Jelu's open reading event when finishing.

    Jelu 0.88 only moves a book's status to an event dated after its last one; a
    FINISHED event dated at or before the open reading event is stored, but the book
    stays "reading", and the next run would send it again.
    """
    when = title.last_read
    if event == FINISHED and book is not None and book.last_event == READING:
        after = book.last_event_at
        if after is not None and (when is None or when <= after):
            when = after + timedelta(seconds=1)
    return when


# --- matching ---


def title_key(title: str) -> str:
    """Case, accents, punctuation, a leading article and the subtitle set aside."""
    text = re.sub(r"['’]", "", title)  # "Schrödinger’s" and "Schrodingers" alike
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.split(r"\s*[:(]|\s+[-–—]\s+", text, maxsplit=1)[0]
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"^(the|a|an) ", "", re.sub(r"\s+", " ", text).strip())
    return text


def whole_title(title: str) -> str:
    """The whole title, subtitle and all, with only case and punctuation set aside."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _surnames(authors: Iterable[str]) -> set[str]:
    out = set()
    for name in authors:
        words = re.findall(
            r"[a-z]+",
            unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower(),
        )
        if words:
            out.add(words[-1])
    return out


def match(title: KoboTitle, books: list[JeluBook]) -> tuple[JeluBook | None, str | None]:
    """The Jelu book this Kobo title is, and how it matched; (book, "title only") is weak."""
    if title.isbn13:
        by_isbn = [b for b in books if title.isbn13 in b.isbns]
        if by_isbn:
            # Your own copy first, if Jelu holds the same ISBN twice
            by_isbn.sort(key=lambda b: b.userbook_id is None)
            return by_isbn[0], "isbn"
    key = title_key(title.title)
    if not key:
        return None, None
    same = [b for b in books if title_key(b.title) == key]
    if not same:
        return None, None
    surnames = _surnames(title.authors)
    strong = [b for b in same if surnames & _surnames(b.authors)]
    if len(strong) == 1:
        return strong[0], "title and author"
    if not surnames:
        # Neither names an author, as with a paper pushed with --title: the title is all
        # there is on both sides
        whole = whole_title(title.title)
        bare = [b for b in same if not b.authors and whole_title(b.title) == whole]
        if len(bare) == 1:
            return bare[0], "title, no authors"
    return same[0], "title only"


# --- LeStash links ---


def _item_ids(conn: sqlite3.Connection, content_ids: Iterable[str]) -> dict[str, int]:
    ids = {}
    for content_id in content_ids:
        row = conn.execute(
            "SELECT id FROM items WHERE source_type = ? AND source_id = ?",
            (SOURCE_TYPE, content_id),
        ).fetchone()
        if row:
            ids[content_id] = row[0]
    return ids


def _jelu_item(conn: sqlite3.Connection, book_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM items WHERE source_type = 'jelu' AND source_id = ?",
        (JELU_BOOK_PREFIX + book_id,),
    ).fetchone()
    return row[0] if row else None


def _rejected(conn: sqlite3.Connection, a: int | None, b: int | None) -> bool:
    if a is None or b is None:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM item_links WHERE kind = 'alternate_of' AND status = 'rejected'"
            " AND ((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            (a, b, b, a),
        ).fetchone()
        is not None
    )


def link_match(conn: sqlite3.Connection, step: Step) -> bool:
    """Record a match as an ``alternate_of`` link, once both items exist. True if added."""
    if step.jelu is None or step.how not in ("isbn", "title and author", "title, no authors"):
        return False
    kobo_id = _item_ids(conn, [step.kobo.content_id]).get(step.kobo.content_id)
    jelu_id = _jelu_item(conn, step.jelu.book_id)
    if kobo_id is None or jelu_id is None:
        return False
    evidence = (
        {"isbn": step.kobo.isbn13}
        if step.how == "isbn"
        else {"title": step.kobo.title, "authors": step.kobo.authors}
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, status)"
        " VALUES (?, ?, 'alternate_of', ?, ?, 'confirmed')",
        (kobo_id, jelu_id, json.dumps(evidence), LINK_ORIGIN),
    )
    return cursor.rowcount > 0


# --- the plan ---


def _event_for(kobo_status: str, jelu_event: str | None) -> tuple[str, str | None]:
    """(action, event) moving Jelu's status forward to the Kobo's, never back."""
    if kobo_status == "reading":
        if jelu_event is None:
            return START, READING
        if jelu_event == READING:
            return NOTHING, None
        return CONFLICT, None  # finished or dropped in Jelu
    if jelu_event in (None, READING):
        return FINISH, FINISHED
    if jelu_event == FINISHED:
        return NOTHING, None
    return CONFLICT, None  # dropped in Jelu


def plan(
    conn: sqlite3.Connection,
    titles: list[KoboTitle],
    books: list[JeluBook],
    also: set[str] | None = None,
) -> Plan:
    """What pushing would do. ``also``: Kobo ContentIDs to push though they aren't books."""
    also = also or set()
    kobo_items = _item_ids(conn, (t.content_id for t in titles))
    result = Plan()
    for title in titles:
        title.item_id = kobo_items.get(title.content_id)
        book, how = match(title, books)
        if book and _rejected(
            conn, kobo_items.get(title.content_id), _jelu_item(conn, book.book_id)
        ):
            result.steps.append(Step(REJECTED, title, book, how))
            continue
        if book and how == "title only":
            result.steps.append(Step(WEAK, title, book, how))
            continue
        if book is None:
            if title.isbn13 or title.content_id in also:
                event = READING if title.status == "reading" else FINISHED
                result.steps.append(Step(CREATE, title, None, None, event, title.last_read))
            else:
                result.steps.append(Step(LEFT_OUT, title))
            continue
        action, sent = _event_for(title.status, book.last_event)
        when = event_time(title, book, sent) if sent else None
        result.steps.append(Step(action, title, book, how, sent, when))
    _one_change_per_book(result)
    return result


def _book_key(step: Step) -> str:
    if step.jelu is not None:
        return f"jelu:{step.jelu.book_id}"
    if step.kobo.isbn13:
        return f"isbn:{step.kobo.isbn13}"
    # The whole title, subtitle and all: a paper pushed with --title has no ISBN, and two
    # documents may share the start of their titles
    return f"title:{whole_title(step.kobo.title)}:{sorted(_surnames(step.kobo.authors))}"


def _one_change_per_book(todo: Plan) -> None:
    """Two Kobo titles of one book (two copies, or one ISBN twice) send one change.

    The furthest status wins (finished over reading), then the latest read; the other
    becomes SAME_BOOK, so no book is created twice and no event is sent twice.
    """
    rank = {READING: 1, FINISHED: 2}
    best: dict[str, Step] = {}
    for step in todo.changes:
        key = _book_key(step)
        kept = best.get(key)
        if kept is None:
            best[key] = step
            continue
        mine = (rank.get(step.event or "", 0), step.when or datetime.min.replace(tzinfo=UTC))
        theirs = (rank.get(kept.event or "", 0), kept.when or datetime.min.replace(tzinfo=UTC))
        loser = kept if mine > theirs else step
        if loser is kept:
            best[key] = step
        loser.action = SAME_BOOK


def when_text(when: datetime | None) -> str | None:
    if when is None:
        return None
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply(conn: sqlite3.Connection, client: Any, todo: Plan) -> tuple[int, int, int, list[str]]:
    """Make the plan's changes in Jelu, and link each match in LeStash.

    Returns (books created, events sent, links added, errors). One failed book doesn't
    stop the rest.
    """
    created = events = links = 0
    errors: list[str] = []
    for step in todo.steps:
        if step.action in (START, FINISH, CREATE):
            try:
                if step.action == CREATE:
                    book = client.add_book(
                        step.kobo.title,
                        step.kobo.authors,
                        isbn13=step.kobo.isbn13,
                    )
                    created += 1
                    step.jelu = JeluBook(str(book["id"]), step.kobo.title, step.kobo.authors, set())
                assert step.jelu is not None and step.event is not None
                client.add_reading_event(step.jelu.book_id, step.event, when_text(step.when))
                events += 1
            except Exception as e:  # report it and carry on with the next book
                errors.append(f"{step.kobo.title}: {e}")
                continue
        if step.action in (START, FINISH, NOTHING, CONFLICT, SAME_BOOK) and link_match(conn, step):
            links += 1
    conn.commit()
    return created, events, links, errors

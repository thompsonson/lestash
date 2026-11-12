"""LinkedIn data export: your posts, comments and articles, back to when you joined.

The sync reads LinkedIn's changelog, which only reaches back to when it started. The
export you ask LinkedIn for (Settings → Data privacy → Get a copy of your data) holds
everything since you joined. This importer reads three of its files:

- ``Shares.csv``: your posts, with their text.
- ``Comments.csv``: your comments, with their text and the post each is on. The post's
  own text is not in the export, so a post LeStash doesn't hold gets a stub.
- ``Articles/Articles/*.html``: your articles, and drafts never published.

Reactions, reposts and saved items are only a date and a link to someone else's post,
so they are left out.

Items are stored as the sync stores them (``resource_name`` ugcPosts,
socialActions/comments or article), with ``source: export``, so kinds, parents and stubs
treat both alike. One already held from the sync is not stored twice: a post by its URN,
a comment by its post and its text or time. A held one whose text the changelog never
gave ("CREATE socialActions/comments") gets the export's text.

Export times are UTC.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from lestash.core.database import mark_recent_history, max_history_id
from lestash.core.sync import SyncStore
from lestash.models.item import ItemCreate

from lestash_linkedin.stubs import COMMENT_URN, POST_URN, SAME_POST_MS, HeldPosts, snowflake_ms

SOURCE = "linkedin"
EXPORT = "export"
PULSE = "https://www.linkedin.com/pulse/"
# A comment the changelog made within this of the export's time is the same comment
SAME_COMMENT_MS = 5000


@dataclass
class ImportReport:
    """What one import did, by file."""

    posts_added: int = 0
    posts_held: int = 0  # already held from the sync
    comments_added: int = 0
    comments_held: int = 0
    articles_added: int = 0
    articles_updated: int = 0  # held, now with the export's full text
    filled: int = 0  # held items whose missing text the export gave
    skipped: int = 0  # rows without text or a post link
    files: list[str] = field(default_factory=list)


def parse_export_time(value: str) -> datetime | None:
    """An export time, in UTC: "2024-03-01 10:00:00", or "2024-03-01 10:00"."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def post_urn_of(link: str) -> str | None:
    """The post a feed link names, decoded: .../feed/update/urn%3Ali%3Ashare%3A1/."""
    match = POST_URN.search(unquote(link or ""))
    return match.group(0) if match else None


def _rows(zf: zipfile.ZipFile, name: str, **dialect: Any) -> list[dict[str, str]]:
    found = [n for n in zf.namelist() if PurePosixPath(n).name == name]
    if not found:
        return []
    text = zf.read(found[0]).decode("utf-8-sig")
    csv.field_size_limit(10**8)
    return list(csv.DictReader(io.StringIO(text), **dialect))


def _norm(text: str) -> str:
    """Text compared without spacing, punctuation or case: the changelog and the export
    write the same comment with different spaces and quotes."""
    return re.sub(r"\W+", "", text or "").lower()


def _placeholder(content: str | None, metadata: dict[str, Any]) -> bool:
    """Whether a held item's text is only what the changelog wrote when it had none."""
    text = (content or "").strip()
    return not text or text == f"{metadata.get('method', '')} {metadata.get('resource_name', '')}"


def _metadata(value: str | None) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _your_urn(conn: sqlite3.Connection) -> str | None:
    """Your LinkedIn member URN, as the sync wrote it on your posts."""
    row = conn.execute(
        "SELECT author FROM items WHERE source_type = 'linkedin' AND is_own_content = 1 "
        "AND author LIKE 'urn:li:person:%' GROUP BY author ORDER BY count(*) DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


@dataclass
class _HeldComment:
    item_id: int
    post: str  # the post it is on
    post_ms: int | None  # when that post's URN was made
    text: str
    ms: int | None  # when it was made; None for an edit, which is later
    placeholder: bool


def _post_of(target: str) -> str:
    match = COMMENT_URN.match(target)
    return f"urn:li:{match.group(1)}:{match.group(2)}" if match else target


def _held_comments(conn: sqlite3.Connection) -> list[_HeldComment]:
    """Your held LinkedIn comments: from the sync, an earlier import, or the snapshot
    fetch (``ALL_COMMENTS``). Not others' comments that the changelog logged on your
    posts: their actor is not the owner."""
    held: list[_HeldComment] = []
    rows = conn.execute(
        "SELECT id, content, url, source_id, metadata FROM items "
        "WHERE source_type = 'linkedin' AND held = 1 "
        "AND (json_extract(metadata, '$.resource_name') = 'socialActions/comments' "
        "OR json_extract(metadata, '$.domain') = 'ALL_COMMENTS')"
    )
    for item_id, content, url, source_id, metadata_json in rows:
        metadata = _metadata(metadata_json)
        raw = _dict(metadata.get("raw"))
        actor, owner = raw.get("actor"), raw.get("owner")
        if actor and owner and actor != owner:
            continue
        target = metadata.get("commented_on")
        if metadata.get("domain") == "ALL_COMMENTS":
            target = post_urn_of(url or source_id or "")
        if not isinstance(target, str) or not target:
            continue
        post = _post_of(target)
        ms = _dict(_dict(raw.get("activity")).get("created")).get("time")
        # capturedAt is within a second of the comment; processedAt can be 30 s later
        ms = ms or raw.get("capturedAt") or raw.get("processedAt") or metadata.get("export_ms")
        created = isinstance(ms, int | float) and metadata.get("method", "CREATE") == "CREATE"
        held.append(
            _HeldComment(
                item_id,
                post,
                snowflake_ms(post),
                content or "",
                int(ms) if created and ms is not None else None,
                _placeholder(content, metadata),
            )
        )
    return held


@dataclass
class _CommentRow:
    text: str
    link: str
    post: str
    when: datetime
    ms: int


class _Import:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.report = ImportReport()
        self.posts = HeldPosts(conn)
        self.comments = _held_comments(conn)
        self.matched: set[int] = set()  # held comments an export row has matched
        self.author = _your_urn(conn)
        self.fills: dict[int, str] = {}  # held item -> the export's text
        self.used: set[str] = set()  # comment source ids given this run

    def _held_text(self, item_id: int) -> tuple[str | None, dict[str, Any]]:
        row = self.conn.execute(
            "SELECT content, metadata FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return (row[0], _metadata(row[1])) if row else (None, {})

    def posts_from(self, zf: zipfile.ZipFile) -> Iterator[ItemCreate]:
        for row in _rows(zf, "Shares.csv"):
            text = (row.get("ShareCommentary") or "").strip()
            link = row.get("ShareLink") or ""
            urn = post_urn_of(link)
            when = parse_export_time(row.get("Date", ""))
            if not text or not urn:
                self.report.skipped += 1
                continue
            held = self.posts.find(urn)
            if held is not None:
                self.report.posts_held += 1
                content, metadata = self._held_text(held)
                if _placeholder(content, metadata):
                    self.fills[held] = text
                continue
            self.report.posts_added += 1
            yield ItemCreate(
                source_type=SOURCE,
                source_id=f"export-ugcPosts-{urn}",
                url=link or None,
                content=text,
                author=self.author,
                created_at=when,
                is_own_content=True,
                metadata={
                    "source": EXPORT,
                    "resource_name": "ugcPosts",
                    "post_id": urn,
                    "export_file": "Shares.csv",
                    "shared_url": (row.get("SharedUrl") or "").strip() or None,
                    "media_url": (row.get("MediaUrl") or "").strip() or None,
                    "visibility": row.get("Visibility") or None,
                },
            )

    def _on_post(self, post: str) -> list[_HeldComment]:
        """Held comments on this post, not yet matched: one URN, or a share and its
        activity made within SAME_POST_MS."""
        post_ms = snowflake_ms(post)
        return [
            h
            for h in self.comments
            if h.item_id not in self.matched
            and (
                h.post == post
                or (
                    post_ms is not None
                    and h.post_ms is not None
                    and abs(h.post_ms - post_ms) <= SAME_POST_MS
                )
            )
        ]

    def _match_comments(self, rows: list[_CommentRow]) -> dict[int, _HeldComment]:
        """Which export rows are held comments, by row index. Same text first, closest in
        time first, over every row; then the closest times within SAME_COMMENT_MS. Each
        held comment matches one row."""
        found: dict[int, _HeldComment] = {}
        # Same text, closest time first. Text that is only emoji or punctuation is no
        # evidence, so it matches by time only.
        same_text = sorted(
            (abs((h.ms if h.ms is not None else row.ms) - row.ms), i, h.item_id, h)
            for i, row in enumerate(rows)
            if (wanted := _norm(row.text))
            for h in self._on_post(row.post)
            if not h.placeholder and _norm(h.text) == wanted
        )
        for _, i, item_id, held in same_text:
            if i in found or item_id in self.matched:
                continue
            found[i] = held
            self.matched.add(item_id)
        pairs = sorted(
            (abs(h.ms - row.ms), i, h.item_id, h)
            for i, row in enumerate(rows)
            if i not in found
            for h in self._on_post(row.post)
            if h.ms is not None and abs(h.ms - row.ms) < SAME_COMMENT_MS
        )
        for _, i, item_id, held in pairs:
            if i in found or item_id in self.matched:
                continue
            found[i] = held
            self.matched.add(item_id)
        return found

    def comments_from(self, zf: zipfile.ZipFile) -> Iterator[ItemCreate]:
        rows: list[_CommentRow] = []
        # The export escapes quotes in comments with a backslash
        for record in _rows(zf, "Comments.csv", escapechar="\\"):
            text = (record.get("Message") or "").strip()
            link = record.get("Link") or ""
            post = post_urn_of(link)
            when = parse_export_time(record.get("Date", ""))
            if not text or not post or when is None:
                self.report.skipped += 1
                continue
            rows.append(_CommentRow(text, link, post, when, int(when.timestamp() * 1000)))
        matched = self._match_comments(rows)
        for i, row in enumerate(rows):
            held = matched.get(i)
            if held is not None:
                self.report.comments_held += 1
                if held.placeholder:
                    self.fills[held.item_id] = row.text
                continue
            self.report.comments_added += 1
            # The export gives no comment id: the post and the time name it, and a count
            # tells apart two comments on one post in the same second
            source_id, n = f"export-comments-{row.post}-{row.ms}", 1
            while source_id in self.used:
                n += 1
                source_id = f"export-comments-{row.post}-{row.ms}-{n}"
            self.used.add(source_id)
            yield ItemCreate(
                source_type=SOURCE,
                source_id=source_id,
                url=row.link or None,
                content=row.text,
                author=self.author,
                created_at=row.when,
                is_own_content=True,
                metadata={
                    "source": EXPORT,
                    "resource_name": "socialActions/comments",
                    "commented_on": row.post,
                    "export_file": "Comments.csv",
                    "export_ms": row.ms,
                },
            )

    def articles_from(self, zf: zipfile.ZipFile) -> Iterator[ItemCreate]:
        for name in sorted(zf.namelist()):
            path = PurePosixPath(name)
            if path.suffix != ".html" or "Articles" not in path.parts:
                continue
            article = parse_article(zf.read(name).decode("utf-8"), path.name)
            if article is None:
                self.report.skipped += 1
                continue
            source_id = article.url or f"export-article-{path.stem}"
            stored = self._held_article(article.url or source_id)
            metadata: dict[str, Any] = {}
            if stored is not None:
                source_id, metadata = stored
                self.report.articles_updated += 1
            else:
                self.report.articles_added += 1
            metadata.update(
                {
                    "source": EXPORT,
                    "resource_name": "article",
                    "export_file": name,
                    "created_on": article.created_on,
                    "published_on": article.published_on,
                    "draft": article.published is None,
                }
            )
            yield ItemCreate(
                source_type=SOURCE,
                source_id=source_id,
                url=article.url,
                title=article.title,
                content=article.text,
                author=self.author,
                created_at=article.published or article.created,
                is_own_content=True,
                metadata=metadata,
            )

    def _held_article(self, url: str) -> tuple[str, dict[str, Any]] | None:
        """An article held already, such as one added by hand, by its link (or, for a
        draft, its source id): its source id and metadata."""
        row = self.conn.execute(
            "SELECT source_id, metadata FROM items WHERE source_type = 'linkedin' "
            "AND (rtrim(source_id, '/') = ? OR rtrim(url, '/') = ?) ORDER BY id LIMIT 1",
            (url, url),
        ).fetchone()
        return (row[0], _metadata(row[1])) if row else None

    def fill(self) -> None:
        """Give held items the export's text where the changelog gave none."""
        since = max_history_id(self.conn)
        for item_id, text in self.fills.items():
            self.conn.execute("UPDATE items SET content = ? WHERE id = ?", (text, item_id))
        self.report.filled = len(self.fills)
        mark_recent_history(self.conn, since, "linkedin export", item_ids=list(self.fills))


@dataclass
class Article:
    title: str
    text: str
    url: str | None
    created: datetime | None
    published: datetime | None
    created_on: str | None
    published_on: str | None


def parse_article(html: str, filename: str) -> Article | None:
    """An article page from the export: its title, link, times and text as Markdown."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    link = heading.find("a") if heading else None
    href = link.get("href") if link else None
    url = str(href).rstrip("/") if isinstance(href, str) and href.startswith(PULSE) else None

    def stamp(css: str) -> str | None:
        tag = soup.find(class_=css)
        found = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", tag.get_text()) if tag else None
        return found.group(0) if found else None

    created_on, published_on = stamp("created"), stamp("published")
    # The first heading is the title; later ones are the article's own sections
    for tag in [heading, *soup.find_all(["style", "title"])] + soup.find_all(
        class_=["created", "published"]
    ):
        if tag is not None:
            tag.decompose()
    body = soup.body or soup
    text = markdownify(str(body), heading_style="ATX").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return None
    return Article(
        title=title or PurePosixPath(filename).stem,
        text=text,
        url=url,
        created=parse_export_time(created_on or ""),
        published=parse_export_time(published_on or ""),
        created_on=created_on,
        published_on=published_on,
    )


def import_export(conn: sqlite3.Connection, zip_path: Path) -> ImportReport:
    """Import your posts, comments and articles from a LinkedIn export ZIP, then link
    comments to their posts. Safe to run again. Commits."""
    from lestash_linkedin.source import resolve_linkedin_parents

    run = _Import(conn)
    with zipfile.ZipFile(zip_path) as zf:
        names = {PurePosixPath(n).name for n in zf.namelist()}
        if "Shares.csv" not in names and "Comments.csv" not in names:
            raise ValueError("Not a LinkedIn export: no Shares.csv or Comments.csv in it")
        items = [*run.posts_from(zf), *run.comments_from(zf), *run.articles_from(zf)]
    store = SyncStore(conn, SOURCE)
    store.run(items)
    run.fill()
    conn.commit()
    resolve_linkedin_parents(conn)  # comments to their posts; stubs for posts not held
    return run.report

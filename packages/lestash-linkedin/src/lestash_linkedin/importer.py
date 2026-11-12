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

from lestash_linkedin.stubs import COMMENT_URN, HeldPosts

SOURCE = "linkedin"
EXPORT = "export"
# The post a link names: urn:li:share:1, urn:li:ugcPost:1, urn:li:activity:1, or
# urn:li:groupPost:<group>-<id>
POST_URN = re.compile(r"urn:li:(?:share|ugcPost|activity|groupPost):[\d-]+")
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
    text: str
    ms: int | None
    placeholder: bool


def _held_comments(conn: sqlite3.Connection) -> dict[str, list[_HeldComment]]:
    """Held LinkedIn comments, from the sync or an earlier import, by the post each is on."""
    held: dict[str, list[_HeldComment]] = {}
    rows = conn.execute(
        "SELECT id, content, metadata FROM items WHERE source_type = 'linkedin' AND held = 1 "
        "AND json_extract(metadata, '$.resource_name') = 'socialActions/comments'"
    )
    for item_id, content, metadata_json in rows:
        metadata = _metadata(metadata_json)
        target = metadata.get("commented_on")
        if not isinstance(target, str):
            continue
        match = COMMENT_URN.match(target)
        post = f"urn:li:{match.group(1)}:{match.group(2)}" if match else target
        raw = _dict(metadata.get("raw"))
        ms = _dict(_dict(raw.get("activity")).get("created")).get("time")
        # capturedAt is within a second of the comment; processedAt can be 30 s later
        ms = ms or raw.get("capturedAt") or raw.get("processedAt") or metadata.get("export_ms")
        held.setdefault(post, []).append(
            _HeldComment(
                item_id,
                content or "",
                # An edit is made later than its comment: only its text can match
                int(ms)
                if isinstance(ms, int | float) and metadata.get("method", "CREATE") == "CREATE"
                else None,
                _placeholder(content, metadata),
            )
        )
    return held


class _Import:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.report = ImportReport()
        self.posts = HeldPosts(conn)
        self.comments = _held_comments(conn)
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

    def _held_comment(self, post: str, text: str, ms: int | None) -> _HeldComment | None:
        for held in self.comments.get(post, []):
            if not held.placeholder and _norm(held.text) == _norm(text):
                return held
            if ms is not None and held.ms is not None and abs(held.ms - ms) < SAME_COMMENT_MS:
                return held
        return None

    def comments_from(self, zf: zipfile.ZipFile) -> Iterator[ItemCreate]:
        # The export escapes quotes in comments with a backslash
        for row in _rows(zf, "Comments.csv", escapechar="\\"):
            text = (row.get("Message") or "").strip()
            link = row.get("Link") or ""
            post = post_urn_of(link)
            when = parse_export_time(row.get("Date", ""))
            if not text or not post or when is None:
                self.report.skipped += 1
                continue
            ms = int(when.timestamp() * 1000)
            held = self._held_comment(post, text, ms)
            if held is not None:
                self.report.comments_held += 1
                if held.placeholder:
                    self.fills[held.item_id] = text
                continue
            self.report.comments_added += 1
            # The export gives no comment id: the post and the time name it, and a count
            # tells apart two comments on one post in the same second
            source_id, n = f"export-comments-{post}-{ms}", 1
            while source_id in self.used:
                n += 1
                source_id = f"export-comments-{post}-{ms}-{n}"
            self.used.add(source_id)
            yield ItemCreate(
                source_type=SOURCE,
                source_id=source_id,
                url=link or None,
                content=text,
                author=self.author,
                created_at=when,
                is_own_content=True,
                metadata={
                    "source": EXPORT,
                    "resource_name": "socialActions/comments",
                    "commented_on": post,
                    "export_file": "Comments.csv",
                    "export_ms": ms,
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
    for tag in soup.find_all(["h1", "style", "title"]) + soup.find_all(
        class_=["created", "published"]
    ):
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

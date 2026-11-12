"""Photos and documents from a LinkedIn export, onto the posts and articles they were
uploaded for.

``Rich_Media.csv`` lists each file you uploaded: when ("You uploaded a feed photo on
June 21, 2024 at 5:26 PM (GMT)"), a description, and a link. It doesn't say which post
a file belongs to, so:

- A description that is a post's text (as LinkedIn wrote since 2026) names the post.
- Otherwise a feed photo or document is the post you made within FEED_WINDOW after the
  upload, the nearest if several; an article photo is the article published within
  ARTICLE_WINDOW after it.
- Plain "photo" uploads (profile and banner pictures) belong to no post: left out.
- Videos are streaming playlists, not files: left out.

The links are signed and expire within weeks of the export, so each file is fetched
into the file store when imported. A file a post holds already is not added twice.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from lestash.core.blobs import BlobStore, record_blob
from lestash.core.database import add_item_media

logger = logging.getLogger(__name__)
ORIGIN = "linkedin export"
UPLOADED = re.compile(r"^You uploaded an? (?P<kind>.+?) on (?P<when>.+?) \(GMT\)\s*$")
# Uploads come before their post: the gap seen is 0 to 10 minutes
FEED_WINDOW = (timedelta(minutes=-2), timedelta(minutes=15))
ARTICLE_WINDOW = (timedelta(minutes=-2), timedelta(minutes=30))
FEED_KINDS = ("feed photo", "feed document")
ARTICLE_KINDS = ("article cover photo", "article inline photo")
MAX_BYTES = 200 * 1024 * 1024

# (url) -> (bytes, content type); raises on failure
Fetch = Callable[[str], tuple[bytes, str]]


@dataclass
class MediaReport:
    added: int = 0
    held: int = 0  # the post holds this file already
    unmatched: int = 0  # no post or article fits
    no_link: int = 0  # the export gives no link to the file
    not_post_media: int = 0  # profile and banner photos
    videos: int = 0  # playlists, not files
    failed: int = 0  # the download failed; tried again next run


@dataclass
class Upload:
    kind: str
    when: datetime
    description: str
    link: str


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text or "").lower()


def _time(value: str) -> datetime | None:
    """A stored created_at, as UTC. One without a zone is taken as UTC."""
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_uploads(zf: zipfile.ZipFile) -> list[Upload]:
    """The uploads Rich_Media.csv lists; [] if the export has none."""
    found = [n for n in zf.namelist() if PurePosixPath(n).name == "Rich_Media.csv"]
    if not found:
        return []
    text = zf.read(found[0]).decode("utf-8-sig")
    uploads = []
    for row in csv.DictReader(io.StringIO(text)):
        match = UPLOADED.match(row.get("Date/Time") or "")
        if not match:
            continue
        try:
            when = datetime.strptime(match["when"], "%B %d, %Y at %I:%M %p").replace(tzinfo=UTC)
        except ValueError:
            continue
        uploads.append(
            Upload(
                match["kind"],
                when,
                (row.get("Media Description") or "").strip(),
                (row.get("Media Link") or "").strip(),
            )
        )
    return uploads


@dataclass
class _Target:
    item_id: int
    at: datetime
    text: str


def _targets(conn: sqlite3.Connection, resource: str) -> list[_Target]:
    rows = conn.execute(
        "SELECT id, created_at, content FROM items WHERE source_type = 'linkedin' "
        "AND held = 1 AND is_own_content = 1 AND json_extract(metadata, '$.resource_name') = ?",
        (resource,),
    )
    targets = []
    for item_id, created_at, content in rows:
        at = _time(created_at)
        if at is not None:
            targets.append(_Target(item_id, at, _norm(content or "")))
    return targets


def _in_window(
    targets: list[_Target], when: datetime, window: tuple[timedelta, timedelta]
) -> int | None:
    near = [t for t in targets if window[0] <= t.at - when <= window[1]]
    return min(near, key=lambda t: abs(t.at - when)).item_id if near else None


def match_upload(upload: Upload, posts: list[_Target], articles: list[_Target]) -> int | None:
    """The post or article an upload belongs to, if one fits."""
    wanted = _norm(upload.description)
    if len(wanted) >= 20:
        named = [p for p in posts if p.text.startswith(wanted[:60])]
        if named:
            return min(named, key=lambda p: abs(p.at - upload.when)).item_id
    if upload.kind in FEED_KINDS:
        return _in_window(posts, upload.when, FEED_WINDOW)
    if upload.kind in ARTICLE_KINDS:
        return _in_window(articles, upload.when, ARTICLE_WINDOW)
    return None


def fetch_url(url: str) -> tuple[bytes, str]:
    import httpx

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError(f"larger than {MAX_BYTES} bytes")
        return response.content, response.headers.get("content-type", "")


def _media_type(content_type: str) -> str:
    mime = content_type.split(";")[0].strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    return "file"


def import_media(
    conn: sqlite3.Connection, zf: zipfile.ZipFile, store: BlobStore, fetch: Fetch = fetch_url
) -> MediaReport:
    """Fetch each upload into the file store and attach it to its post or article.
    Safe to run again. Commits."""
    report = MediaReport()
    posts, articles = _targets(conn, "ugcPosts"), _targets(conn, "article")
    position: dict[int, int] = {}
    # Oldest first, so a post's photos keep the order they were uploaded in
    for upload in sorted(read_uploads(zf), key=lambda u: u.when):
        if upload.kind == "video":
            report.videos += 1
            continue
        if upload.kind not in FEED_KINDS + ARTICLE_KINDS and len(_norm(upload.description)) < 20:
            report.not_post_media += 1
            continue
        if not upload.link:
            report.no_link += 1
            continue
        item_id = match_upload(upload, posts, articles)
        if item_id is None:
            report.unmatched += 1
            continue
        try:
            data, content_type = fetch(upload.link)
            blob = store.put_stream(io.BytesIO(data))
        except Exception as e:
            logger.warning("Could not fetch or store the %s of %s: %s", upload.kind, upload.when, e)
            report.failed += 1
            continue
        held = conn.execute(
            "SELECT 1 FROM item_media WHERE item_id = ? AND sha256 = ?", (item_id, blob.sha256)
        ).fetchone()
        position[item_id] = position.get(item_id, 0) + 1
        if held:
            report.held += 1
            continue
        mime = content_type.split(";")[0].strip() or None
        record_blob(conn, blob, mime)
        # A description that is the post's text says nothing more about the photo
        description = upload.description
        is_text = any(
            p.item_id == item_id
            and _norm(description)[:60]
            and p.text.startswith(_norm(description)[:60])
            for p in posts
        )
        alt = None if is_text or description in ("", "-") or len(description) > 500 else description
        add_item_media(
            conn,
            item_id,
            media_type=_media_type(content_type),
            mime_type=mime,
            alt_text=alt,
            position=position[item_id] - 1,
            source_origin=ORIGIN,
            sha256=blob.sha256,
            size=blob.size,
            _commit=False,
        )
        report.added += 1
    conn.commit()
    return report

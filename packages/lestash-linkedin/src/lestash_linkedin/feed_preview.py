"""Cache previews of posts you've engaged with, for the engagement feed UI.

LinkedIn's `/rest/posts/{urn}` endpoint is gated to Marketing Partners, and the
self-serve DMA tokens can't fetch arbitrary posts. But `/feed/update/urn:li:<kind>:<id>/`
serves an *unauthenticated* public preview with Open Graph meta tags. That's
enough to recover the author and a short preview for posts you liked, commented
on, or reposted — which otherwise render as opaque URN stubs.

`cache_engaged_posts()` walks engagement-target URNs that aren't cached yet,
fetches each preview, and upserts a `post_cache` row with ``source='feed_preview'``.
It is idempotent: already-cached URNs are skipped, so it's safe to run on every
sync (bounded by ``limit``) and again as a one-off backfill.

ToS caveat: this scrapes the public preview. No auth bypass. Rate-limited by
``sleep`` seconds per URL; stops early on HTTP 429.

Only ``content_preview`` (the OG description) is captured, never ``full_content``
— the OG description is itself truncated by LinkedIn.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]+)"')
OG_URL_RE = re.compile(r'<meta property="og:url" content="([^"]+)"')
# /posts/<handle>_<slug>-activity-<id>-<suffix>  — handle is the author's vanity URL
HANDLE_FROM_OGURL_RE = re.compile(r"linkedin\.com/posts/([^_/]+)_")
# Title shape A: "… | <Author> posted on the topic | LinkedIn"
AUTHOR_POSTED_ON_RE = re.compile(r"\|\s*([^|]+?)\s+posted on", re.IGNORECASE)
# Title shape B/C: author appears as a standalone pipe segment, typically TWICE.
PIPE_SEGMENT_RE = re.compile(r"\s*\|\s*")
# Extract the underlying activity ID from a urn:li:comment:(activity:X,Y) compound URN
COMMENT_ACTIVITY_RE = re.compile(r"urn:li:comment:\(activity:(\d+),\d+\)")

DEFAULT_KINDS = ("reacted_to", "commented_on", "reposted_ugc")


def _extract_author(title: str | None, handle: str | None) -> str | None:
    """Pull a human author name out of og:title, with og:url's handle as fallback."""
    if title:
        clean = title.replace("&amp;#39;", "'").replace("&#39;", "'").replace("&amp;", "&")
        m = AUTHOR_POSTED_ON_RE.search(clean)
        if m:
            return m.group(1).strip()
        # Pipe-segment heuristic: split, drop empties and suffixes like "N comments"
        # / "LinkedIn", keep candidates that look like a person name (1-4 words).
        segments = [s.strip() for s in PIPE_SEGMENT_RE.split(clean) if s.strip()]
        cands = [
            s
            for s in segments
            if 1 <= len(s.split()) <= 4
            and not s.lower().endswith("comments")
            and not s.lower().endswith("reactions")
            and s.lower() != "linkedin"
        ]
        # Prefer one that appears more than once (author is repeated in B/C forms)
        for s in cands:
            if cands.count(s) >= 2:
                return s
        # Fallback to the em-dash-then-name pattern: "<title> — <Name>"
        for sep in (" — ", " – ", " - "):
            if sep in clean:
                tail = clean.split(sep, 1)[1]
                tail_first = PIPE_SEGMENT_RE.split(tail, 1)[0].strip()
                if 1 <= len(tail_first.split()) <= 4:
                    return tail_first
    # Last resort: derive from the URL handle (e.g. "benedictevans")
    return handle


def fetch_preview(activity_id: str, urn_kind: str = "activity") -> dict[str, str | None]:
    """Fetch /feed/update/urn:li:<kind>:<id>/ unauthenticated; return og fields.

    `urn_kind` selects the URN namespace: 'activity' (default), 'ugcPost',
    'share'. LinkedIn redirects all three to the canonical /posts/ URL when
    public; the og:* meta tags are the same.

    Returns a dict with: status ("200"/"ERR"/other), title, author, handle,
    og_url, description.
    """
    url = f"https://www.linkedin.com/feed/update/urn:li:{urn_kind}:{activity_id}/"
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310 (https URL only)
            html = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except Exception as e:
        return {
            "status": "ERR",
            "error": str(e),
            "title": None,
            "author": None,
            "description": None,
        }

    def _g(r: re.Pattern[str]) -> str | None:
        m = r.search(html)
        return m.group(1) if m else None

    title = _g(OG_TITLE_RE)
    desc = _g(OG_DESC_RE)
    og_url = _g(OG_URL_RE)
    handle = None
    if og_url:
        m = HANDLE_FROM_OGURL_RE.search(og_url)
        if m:
            handle = m.group(1)
    return {
        "status": str(status),
        "title": title,
        "author": _extract_author(title, handle),
        "handle": handle,
        "og_url": og_url,
        "description": desc,
    }


def _collect_target_urns(conn: sqlite3.Connection, kinds: tuple[str, ...]) -> dict[str, list[str]]:
    """Return distinct engagement-target URNs grouped by engagement kind."""
    queries = {
        "reacted_to": """
            SELECT DISTINCT json_extract(metadata, '$.reacted_to')
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='socialActions/likes'
              AND json_extract(metadata, '$.reacted_to') IS NOT NULL
        """,
        "commented_on": """
            SELECT DISTINCT json_extract(metadata, '$.commented_on')
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='socialActions/comments'
              AND json_extract(metadata, '$.commented_on') IS NOT NULL
        """,
        "reposted_ugc": """
            SELECT DISTINCT json_extract(metadata, '$.raw.activity.repostedContent.ugcPost')
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='instantReposts'
              AND json_extract(metadata, '$.raw.activity.repostedContent.ugcPost') IS NOT NULL
        """,
    }
    return {k: [r[0] for r in conn.execute(queries[k]).fetchall()] for k in kinds}


def _urn_to_fetchable_id(urn: str) -> tuple[str, str] | None:
    """Map an engagement-target URN to (urn_kind, id) for /feed/update/<kind>:<id>/.

    Returns None for URN shapes /feed/update can't open (e.g. groupPost, which is
    private to group members).
    """
    if urn.startswith("urn:li:activity:"):
        return ("activity", urn.split(":")[-1])
    if urn.startswith("urn:li:ugcPost:"):
        return ("ugcPost", urn.split(":")[-1])
    m = COMMENT_ACTIVITY_RE.match(urn)
    if m:
        # Comment URNs: fetch the parent activity, but upsert under the original
        # (compound) URN so joins against metadata.commented_on still work.
        return ("activity", m.group(1))
    return None


def _already_cached_urns(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT urn FROM post_cache").fetchall()}


def build_worklist(
    conn: sqlite3.Connection, kinds: tuple[str, ...] = DEFAULT_KINDS
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Build the deduplicated list of (urn, kind) still needing a cached preview.

    Returns (worklist, scope) where scope summarises counts for reporting.
    """
    targets = _collect_target_urns(conn, kinds)
    cached = _already_cached_urns(conn)

    seen: set[str] = set()
    worklist: list[tuple[str, str]] = []
    skipped_unfetchable = 0
    for kind in kinds:
        for urn in targets[kind]:
            if urn in seen or urn in cached:
                continue
            if _urn_to_fetchable_id(urn) is None:
                skipped_unfetchable += 1
                continue
            seen.add(urn)
            worklist.append((urn, kind))

    scope = {
        "already_cached": len(cached),
        "skipped_unfetchable": skipped_unfetchable,
        "to_fetch": len(worklist),
    }
    scope.update({f"distinct_{k}": len(targets[k]) for k in kinds})
    return worklist, scope


def cache_engaged_posts(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
    sleep: float = 1.0,
    kinds: tuple[str, ...] = DEFAULT_KINDS,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Fetch and cache previews for engaged-with posts that aren't cached yet.

    Idempotent: only fetches URNs absent from ``post_cache``. Bounded by ``limit``
    (0 = no cap). Stops early on HTTP 429. Safe to call inside a sync hook.

    Args:
        conn: Open DB connection (post_cache + items live here).
        limit: Max URNs to fetch this run (0 = all).
        sleep: Seconds between fetches (be gentle on LinkedIn).
        kinds: Engagement kinds to process.
        dry_run: Build the worklist and report scope without fetching/writing.
        on_progress: Optional callback for human-readable progress lines.

    Returns:
        Dict of counts: to_fetch, ok, miss, err, skipped_unfetchable, already_cached.
    """
    from lestash.core.database import upsert_post_cache

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    worklist, scope = build_worklist(conn, kinds)
    if limit:
        worklist = worklist[:limit]

    result = {**scope, "ok": 0, "miss": 0, "err": 0, "fetched": len(worklist)}
    if dry_run or not worklist:
        return result

    ok = miss = err = 0
    for i, (urn, _kind) in enumerate(worklist, 1):
        kind_id = _urn_to_fetchable_id(urn)
        assert kind_id is not None  # filtered in build_worklist
        urn_kind, fid = kind_id
        preview = fetch_preview(fid, urn_kind=urn_kind)
        status = preview.get("status")

        if status == "429":
            _emit(f"[{i}/{len(worklist)}] 429 rate-limited — stopping early")
            break
        if status == "ERR":
            err += 1
            _emit(f"[{i}/{len(worklist)}] ERR {preview.get('error', '?')} {urn}")
        elif status != "200":
            miss += 1
            _emit(f"[{i}/{len(worklist)}] {status} {urn}")
        else:
            og_url = preview.get("og_url") or ""
            # When the post is deleted/private/inaccessible, LinkedIn serves the
            # generic home page meta (og:url without /posts/). Leave it uncached
            # rather than fill it with "500 million+ members | …".
            if "/posts/" not in og_url:
                miss += 1
                _emit(f"[{i}/{len(worklist)}] generic-fallback {urn}")
            else:
                upsert_post_cache(
                    conn,
                    urn=urn,
                    author_name=preview.get("author"),
                    content_preview=preview.get("description"),
                    url=og_url,
                    source="feed_preview",
                )
                ok += 1
        time.sleep(sleep)

    result.update(ok=ok, miss=miss, err=err)
    logger.info("feed_preview cache: ok=%d miss=%d err=%d", ok, miss, err)
    return result

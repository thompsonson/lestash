"""Cache previews of posts you've engaged with, for the engagement feed UI.

LinkedIn's `/rest/posts/{urn}` endpoint is gated to Marketing Partners, and the
self-serve DMA tokens can't fetch arbitrary posts. But `/feed/update/urn:li:<kind>:<id>/`
serves an *unauthenticated* public preview with Open Graph meta tags. That's
enough to recover the author and a short preview for posts you liked, commented
on, or reposted — which otherwise render as opaque URN stubs.

`cache_engaged_posts()` walks engagement-target URNs that aren't cached yet
(newest engagement first), fetches each preview, and upserts a `post_cache` row
with ``source='feed_preview'``. It is idempotent: already-cached URNs are
skipped, so it's safe to run on every sync (bounded by ``limit``) and again as a
one-off backfill. Deleted/private posts are recorded with a sentinel
``source='feed_preview_gone'`` so they aren't re-fetched on every run.

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
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict
from urllib.error import HTTPError
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

DEFAULT_KINDS: tuple[str, ...] = ("reacted_to", "commented_on", "reposted_ugc")

CACHE_SOURCE = "feed_preview"
# Sentinel for deleted/private posts: keeps them out of future worklists without
# pretending we have a real preview (content_preview stays NULL).
GONE_SOURCE = "feed_preview_gone"

# Defaults for the bounded job that runs inside each sync.
SYNC_DEFAULT_LIMIT = 40
SYNC_DEFAULT_SLEEP = 1.0


class Preview(TypedDict):
    """Result of scraping a post's public OG preview. Keys are always present."""

    status: str  # "200", an HTTP error code like "429", or "ERR" for transport errors
    title: str | None
    author: str | None
    handle: str | None
    og_url: str | None
    description: str | None
    error: str | None


class CacheStats(TypedDict):
    """Outcome counts from a cache_engaged_posts() run."""

    already_cached: int
    skipped_unfetchable: int
    to_fetch: int  # full backlog before `limit`
    fetched: int  # actually attempted this run (after `limit`)
    ok: int
    miss: int
    err: int


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


def _preview(status: str, **fields: str | None) -> Preview:
    """Build a Preview with every key present (missing fields default to None)."""
    return Preview(
        status=status,
        title=fields.get("title"),
        author=fields.get("author"),
        handle=fields.get("handle"),
        og_url=fields.get("og_url"),
        description=fields.get("description"),
        error=fields.get("error"),
    )


def fetch_preview(activity_id: str, urn_kind: str = "activity") -> Preview:
    """Fetch /feed/update/urn:li:<kind>:<id>/ unauthenticated; return og fields.

    `urn_kind` selects the URN namespace: 'activity' (default), 'ugcPost',
    'share'. LinkedIn redirects all three to the canonical /posts/ URL when
    public; the og:* meta tags are the same.

    The ``status`` field is the HTTP status as a string ("200"), the HTTP error
    code for a 4xx/5xx ("429", "404", …), or "ERR" for a transport-level failure.
    """
    url = f"https://www.linkedin.com/feed/update/urn:li:{urn_kind}:{activity_id}/"
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310 (https URL only)
            html = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except HTTPError as e:
        # urlopen raises for 4xx/5xx (incl. 429) — surface the code so callers
        # can act on rate limits instead of treating them as generic errors.
        return _preview(str(e.code), error=str(e))
    except Exception as e:
        return _preview("ERR", error=str(e))

    def _g(r: re.Pattern[str]) -> str | None:
        m = r.search(html)
        return m.group(1) if m else None

    title = _g(OG_TITLE_RE)
    og_url = _g(OG_URL_RE)
    handle = None
    if og_url:
        m = HANDLE_FROM_OGURL_RE.search(og_url)
        if m:
            handle = m.group(1)
    return _preview(
        str(status),
        title=title,
        author=_extract_author(title, handle),
        handle=handle,
        og_url=og_url,
        description=_g(OG_DESC_RE),
    )


def _collect_targets(
    conn: sqlite3.Connection, kinds: Sequence[str]
) -> dict[str, list[tuple[str, str | None]]]:
    """Return engagement-target URNs (with latest engagement time) grouped by kind."""
    queries = {
        "reacted_to": """
            SELECT json_extract(metadata, '$.reacted_to') AS urn, MAX(created_at) AS ts
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='socialActions/likes'
              AND json_extract(metadata, '$.reacted_to') IS NOT NULL
            GROUP BY urn
        """,
        "commented_on": """
            SELECT json_extract(metadata, '$.commented_on') AS urn, MAX(created_at) AS ts
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='socialActions/comments'
              AND json_extract(metadata, '$.commented_on') IS NOT NULL
            GROUP BY urn
        """,
        "reposted_ugc": """
            SELECT json_extract(metadata, '$.raw.activity.repostedContent.ugcPost') AS urn,
                   MAX(created_at) AS ts
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name')='instantReposts'
              AND json_extract(metadata, '$.raw.activity.repostedContent.ugcPost') IS NOT NULL
            GROUP BY urn
        """,
    }
    return {k: [(r[0], r[1]) for r in conn.execute(queries[k]).fetchall()] for k in kinds}


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
    """URNs already in post_cache — includes 'gone' sentinels, so they're skipped."""
    return {row[0] for row in conn.execute("SELECT urn FROM post_cache").fetchall()}


def build_worklist(
    conn: sqlite3.Connection, kinds: Sequence[str] = DEFAULT_KINDS
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """Build the (urn, kind) list still needing a preview, newest engagement first.

    Ordering by recency means a bounded run fetches your latest engagements first,
    so a backlog of older/dead URNs can't starve fresh ones. Returns
    (worklist, scope) where scope summarises counts for reporting.
    """
    targets = _collect_targets(conn, kinds)
    cached = _already_cached_urns(conn)

    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []  # (urn, kind, ts)
    skipped_unfetchable = 0
    for kind in kinds:
        for urn, ts in targets[kind]:
            if urn in seen or urn in cached:
                continue
            if _urn_to_fetchable_id(urn) is None:
                skipped_unfetchable += 1
                continue
            seen.add(urn)
            candidates.append((urn, kind, ts or ""))

    # Newest engagement first (ISO timestamps sort lexically; NULLs sort last).
    candidates.sort(key=lambda c: c[2], reverse=True)
    worklist = [(urn, kind) for urn, kind, _ in candidates]

    scope = {
        "already_cached": len(cached),
        "skipped_unfetchable": skipped_unfetchable,
        "to_fetch": len(worklist),
    }
    return worklist, scope


def cache_engaged_posts(
    conn: sqlite3.Connection,
    *,
    limit: int = 0,
    sleep: float = 1.0,
    kinds: Sequence[str] = DEFAULT_KINDS,
    dry_run: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> CacheStats:
    """Fetch and cache previews for engaged-with posts that aren't cached yet.

    Idempotent: only fetches URNs absent from ``post_cache``. Bounded by ``limit``
    (0 = all). Stops early on HTTP 429. Safe to call inside a sync hook.

    Args:
        conn: Open DB connection (post_cache + items live here).
        limit: Max URNs to fetch this run (0 = all).
        sleep: Seconds between fetches (be gentle on LinkedIn).
        kinds: Engagement kinds to process.
        dry_run: Build the worklist and report scope without fetching/writing.
        on_progress: Optional callback for human-readable progress lines.

    Returns:
        CacheStats with the run's outcome counts.
    """
    from lestash.core.database import upsert_post_cache

    def _emit(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    worklist, scope = build_worklist(conn, kinds)
    if limit:
        worklist = worklist[:limit]

    stats = CacheStats(
        already_cached=scope["already_cached"],
        skipped_unfetchable=scope["skipped_unfetchable"],
        to_fetch=scope["to_fetch"],
        fetched=len(worklist),
        ok=0,
        miss=0,
        err=0,
    )
    if dry_run or not worklist:
        return stats

    n = len(worklist)
    for i, (urn, _kind) in enumerate(worklist, 1):
        kind_id = _urn_to_fetchable_id(urn)
        assert kind_id is not None  # filtered in build_worklist
        urn_kind, fid = kind_id
        preview = fetch_preview(fid, urn_kind=urn_kind)
        status = preview["status"]

        if status == "429":
            _emit(f"[{i}/{n}] 429 rate-limited — stopping early")
            break
        if status != "200":
            # Transport error or non-OK HTTP — transient, leave uncached to retry.
            stats["err"] += 1
            _emit(f"[{i}/{n}] {status} {preview['error'] or ''} {urn}".rstrip())
        else:
            og_url = preview["og_url"] or ""
            # Deleted/private/inaccessible posts return LinkedIn's generic home meta
            # (og:url without /posts/). Record a sentinel so we don't re-fetch them
            # every run, rather than caching "500 million+ members | …".
            if "/posts/" not in og_url:
                stats["miss"] += 1
                upsert_post_cache(conn, urn=urn, source=GONE_SOURCE)
                _emit(f"[{i}/{n}] gone/private {urn}")
            else:
                upsert_post_cache(
                    conn,
                    urn=urn,
                    author_name=preview["author"],
                    content_preview=preview["description"],
                    url=og_url,
                    source=CACHE_SOURCE,
                )
                stats["ok"] += 1
        time.sleep(sleep)

    logger.info(
        "feed_preview cache: ok=%d miss=%d err=%d", stats["ok"], stats["miss"], stats["err"]
    )
    return stats


def run_during_sync(
    conn: sqlite3.Connection,
    plugin_config: Mapping[str, Any],
    on_message: Callable[[str], None] | None = None,
) -> CacheStats | None:
    """Run the bounded feed-preview job as part of a LinkedIn sync.

    Reads the optional ``[linkedin.feed_preview]`` config (``enabled`` default
    True, ``limit`` default 40, ``sleep`` default 1.0). Never raises — a scrape
    failure must not fail the sync — returns None when skipped or on error.
    """
    cfg = plugin_config.get("feed_preview", {})
    if not isinstance(cfg, dict):
        cfg = {}
    if not cfg.get("enabled", True):
        return None
    try:
        stats = cache_engaged_posts(
            conn,
            limit=int(cfg.get("limit", SYNC_DEFAULT_LIMIT)),
            sleep=float(cfg.get("sleep", SYNC_DEFAULT_SLEEP)),
        )
    except Exception:
        logger.warning("feed_preview caching skipped due to error", exc_info=True)
        if on_message:
            on_message("Feed-preview caching skipped (see logs)")
        return None
    if on_message and stats["ok"]:
        on_message(f"Cached {stats['ok']} engaged post preview(s)")
    return stats

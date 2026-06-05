#!/usr/bin/env python3
"""One-shot: paste LinkedIn post URLs, get author + any matching engagement in the LeStash DB.

Usage:
    echo "https://www.linkedin.com/posts/benedictevans_...-activity-7417910935379480576-iyKE" \
        | scripts/linkedin_url_lookup.py
    scripts/linkedin_url_lookup.py URL [URL ...]

Why this script exists: LinkedIn's /rest/posts/{urn} endpoint is gated to Marketing
Partners (see ux-compose-and-categories design discussion). Self-serve tokens
(r_dma_portability_self_serve, w_member_social) can't fetch arbitrary posts.

But /feed/update/urn:li:activity:<id>/ serves an unauthenticated public preview
with og:title + og:description populated. That's enough to identify the author
and check whether the LeStash DB shows engagement against that activity URN.

ToS caveat: this scrapes a public preview, no auth bypass. Don't rate-grind.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

DB_PATH = Path.home() / ".config/lestash/lestash.db"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

ACTIVITY_URN_RE = re.compile(r"urn:li:activity:(\d+)")
ACTIVITY_FROM_SLUG_RE = re.compile(r"activity-(\d+)")
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]+)"')
# og:title pattern: "<title> | <Author Name> posted on the topic | LinkedIn"
AUTHOR_FROM_TITLE_RE = re.compile(r"\|\s*([^|]+?)\s+posted on", re.IGNORECASE)


def extract_activity_id(url_or_urn: str) -> str | None:
    """Pull a 19-digit activity ID from a URL slug, /feed/update path, or a URN."""
    s = url_or_urn.strip()
    for r in (ACTIVITY_URN_RE, ACTIVITY_FROM_SLUG_RE):
        m = r.search(s)
        if m:
            return m.group(1)
    return None


def fetch_preview(activity_id: str) -> dict[str, str | None]:
    """Fetch /feed/update/urn:li:activity:<id>/ unauthenticated; return og fields."""
    url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except Exception as e:
        return {"status": "ERR", "error": str(e), "title": None, "author": None, "description": None}

    title = (OG_TITLE_RE.search(html) or [None, None])[1] if OG_TITLE_RE.search(html) else None
    desc = (OG_DESC_RE.search(html) or [None, None])[1] if OG_DESC_RE.search(html) else None
    author = None
    if title:
        # HTML-decode the apostrophe entity LinkedIn uses, then match
        title_clean = title.replace("&amp;#39;", "'").replace("&#39;", "'")
        m = AUTHOR_FROM_TITLE_RE.search(title_clean)
        if m:
            author = m.group(1).strip()
    return {"status": str(status), "title": title, "author": author, "description": desc}


def query_db_for_engagement(activity_id: str) -> list[dict[str, object]]:
    """Find every is_own_content=1 LinkedIn item whose target URN matches this activity ID."""
    urn = f"urn:li:activity:{activity_id}"
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT
              id,
              substr(created_at, 1, 10)                                  AS date,
              json_extract(metadata, '$.resource_name')                  AS kind,
              substr(replace(content, char(10), ' '), 1, 200)            AS snippet,
              json_extract(metadata, '$.reaction_type')                  AS reaction
            FROM items
            WHERE source_type = 'linkedin'
              AND is_own_content = 1
              AND (
                json_extract(metadata, '$.commented_on') = :urn
                OR json_extract(metadata, '$.reacted_to') = :urn
                OR json_extract(metadata, '$.raw.activity.repostedContent.ugcPost') = :urn
              )
            ORDER BY created_at DESC
            """,
            {"urn": urn},
        ).fetchall()
    finally:
        conn.close()
    return [
        {"item_id": r[0], "date": r[1], "kind": r[2], "snippet": r[3], "reaction": r[4]}
        for r in rows
    ]


def process(urls: Iterable[str]) -> None:
    seen: set[str] = set()
    for raw in urls:
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        aid = extract_activity_id(url)
        if not aid:
            print(f"## {url}\n  ⚠ no activity ID found in URL\n")
            continue
        if aid in seen:
            print(f"## activity {aid} (duplicate, skipped)\n")
            continue
        seen.add(aid)

        preview = fetch_preview(aid)
        hits = query_db_for_engagement(aid)

        print(f"## activity {aid}")
        print(f"  preview status: {preview['status']}")
        print(f"  author        : {preview['author'] or '—'}")
        if preview.get("title"):
            print(f"  title         : {preview['title'][:120]}")
        if preview.get("description"):
            print(f"  description   : {preview['description'][:120]}")
        if hits:
            print(f"  ✓ {len(hits)} match(es) in LeStash DB:")
            for h in hits:
                kind = h["kind"]
                react = f" [{h['reaction']}]" if h["reaction"] else ""
                print(f"    - item {h['item_id']} · {h['date']} · {kind}{react}")
                print(f"      {h['snippet']}")
        else:
            print("  · no engagement in DB")
        print()
        time.sleep(1.0)  # gentle on LinkedIn


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        return 2
    if len(sys.argv) > 1:
        process(sys.argv[1:])
    else:
        process(sys.stdin)
    return 0


if __name__ == "__main__":
    sys.exit(main())

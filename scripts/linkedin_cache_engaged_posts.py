#!/usr/bin/env python3
"""Backfill `post_cache` for every post Matt has engaged with (liked, commented
on, reposted), so the UI can render his engagement feed with author + preview
instead of opaque URN stubs.

Walks three sources of engagement-target URNs:
  - metadata.reacted_to                                  (likes)
  - metadata.commented_on                                (comments)
  - metadata.raw.activity.repostedContent.ugcPost        (reposts)

For each distinct, not-yet-cached URN, fetches the unauthenticated
/feed/update preview, extracts og:url (canonical URL + author handle),
og:title (author display name + post title), and og:description (preview text),
and upserts a `post_cache` row with source='feed_preview'.

Read-only without --write. With --write, upserts via `upsert_post_cache`,
which COALESCEs against existing values — your 10 manually-curated rows are
preserved.

ToS caveat: this scrapes the public preview. No auth bypass. Rate-limited at
1s/URL; stops on 429.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Reuse the parser from the per-URL lookup script
from linkedin_url_lookup import fetch_preview  # type: ignore

DB_PATH = Path.home() / ".config/lestash/lestash.db"

# Extract the underlying activity ID from a urn:li:comment:(activity:X,Y) compound URN
COMMENT_ACTIVITY_RE = re.compile(r"urn:li:comment:\(activity:(\d+),\d+\)")


def collect_target_urns(conn: sqlite3.Connection) -> dict[str, list[str]]:
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
    return {
        kind: [r[0] for r in conn.execute(sql).fetchall()]
        for kind, sql in queries.items()
    }


def urn_to_fetchable_id(urn: str) -> tuple[str, str] | None:
    """Map an engagement-target URN to (kind, id) suitable for /feed/update/<kind>:<id>/.

    Returns None for URN shapes /feed/update can't open (groupPost is private to
    members of the group; rare in this corpus).
    """
    if urn.startswith("urn:li:activity:"):
        return ("activity", urn.split(":")[-1])
    if urn.startswith("urn:li:ugcPost:"):
        return ("ugcPost", urn.split(":")[-1])
    m = COMMENT_ACTIVITY_RE.match(urn)
    if m:
        # Comment URNs: fetch the parent activity, but we'll still upsert under the
        # original (compound) URN so joins against metadata.commented_on work.
        return ("activity", m.group(1))
    # groupPost and other shapes — skip
    return None


def already_cached_urns(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT urn FROM post_cache").fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--write",
        action="store_true",
        help="Actually upsert post_cache rows. Without this, the script just "
        "reports the scope and exits.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max URNs to fetch this run (0 = all).",
    )
    ap.add_argument(
        "--kinds",
        nargs="+",
        choices=["reacted_to", "commented_on", "reposted_ugc"],
        default=["reacted_to", "commented_on", "reposted_ugc"],
        help="Which engagement kinds to process.",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds between fetches.",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        targets = collect_target_urns(conn)
        cached = already_cached_urns(conn)

        # Build a single deduplicated worklist preserving kind annotations
        seen: set[str] = set()
        worklist: list[tuple[str, str]] = []  # (urn, kind)
        skipped_unfetchable = 0
        for kind in args.kinds:
            for urn in targets[kind]:
                if urn in seen or urn in cached:
                    continue
                if urn_to_fetchable_id(urn) is None:
                    skipped_unfetchable += 1
                    continue
                seen.add(urn)
                worklist.append((urn, kind))

        # Summary before work starts
        print("# scope")
        for kind in args.kinds:
            print(f"  {kind:14s} distinct in DB: {len(targets[kind])}")
        print(f"  already in post_cache       : {len(cached)}")
        print(f"  skipped (unfetchable URN)   : {skipped_unfetchable}")
        print(f"  to fetch this run           : {len(worklist)}"
              + (f"  (--limit {args.limit})" if args.limit else ""))
        if args.limit:
            worklist = worklist[: args.limit]

        if not args.write:
            print(f"\n# Dry run. Re-run with --write to fetch + cache "
                  f"{len(worklist)} URN(s). Estimated wall-time: "
                  f"~{int(len(worklist) * args.sleep)}s.")
            return 0

        from lestash.core.database import upsert_post_cache

        ok = miss = err = 0
        for i, (urn, kind) in enumerate(worklist, 1):
            kind_id = urn_to_fetchable_id(urn)
            assert kind_id is not None  # filtered above
            _, fid = kind_id
            preview = fetch_preview(fid)
            status = preview.get("status")
            if status == "ERR":
                err += 1
                print(f"[{i}/{len(worklist)}] ERR {preview.get('error','?')} {urn}")
            elif status != "200":
                miss += 1
                print(f"[{i}/{len(worklist)}] {status} {urn}")
            else:
                og_url = preview.get("og_url") or ""
                # When the underlying post is deleted/private/inaccessible, LinkedIn
                # serves the generic home page meta. og:url won't contain /posts/.
                # Skip these — better to leave the URN uncached than to fill it with
                # "500 million+ members | Manage your professional identity…".
                if "/posts/" not in og_url:
                    miss += 1
                    print(f"[{i}/{len(worklist)}] generic-fallback {urn}")
                    time.sleep(args.sleep)
                    continue
                upsert_post_cache(
                    conn,
                    urn=urn,
                    author_name=preview.get("author"),
                    content_preview=preview.get("description"),
                    url=og_url,
                    source="feed_preview",
                )
                ok += 1
                if i % 20 == 0 or i == len(worklist):
                    print(f"[{i}/{len(worklist)}] ok={ok} miss={miss} err={err}")
            time.sleep(args.sleep)

        conn.commit()
        print(f"\n# done. ok={ok}  miss={miss}  err={err}")
        print(f"# post_cache rows after: "
              f"{conn.execute('SELECT COUNT(*) FROM post_cache').fetchone()[0]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

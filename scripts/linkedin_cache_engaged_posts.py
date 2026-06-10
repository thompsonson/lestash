#!/usr/bin/env python3
"""Backfill `post_cache` for posts you've engaged with (liked/commented/reposted).

The logic now lives in the package as `lestash_linkedin.feed_preview` and runs
(bounded) on every sync. This script is a thin CLI wrapper kept for ad-hoc
backfills; prefer `lestash linkedin enrich-feed` for the supported interface.

    scripts/linkedin_cache_engaged_posts.py            # dry run (report scope)
    scripts/linkedin_cache_engaged_posts.py --write    # fetch + cache all gaps
    scripts/linkedin_cache_engaged_posts.py --write --limit 50
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from lestash_linkedin.feed_preview import DEFAULT_KINDS, cache_engaged_posts

DB_PATH = Path.home() / ".config/lestash/lestash.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="Actually upsert (default: dry run).")
    ap.add_argument("--limit", type=int, default=0, help="Max URNs to fetch (0 = all).")
    ap.add_argument(
        "--kinds",
        nargs="+",
        choices=list(DEFAULT_KINDS),
        default=list(DEFAULT_KINDS),
        help="Which engagement kinds to process.",
    )
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between fetches.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        stats = cache_engaged_posts(
            conn,
            limit=args.limit,
            sleep=args.sleep,
            kinds=tuple(args.kinds),
            dry_run=not args.write,
            on_progress=print,
        )
    finally:
        conn.close()

    print(
        f"\n# scope: already_cached={stats['already_cached']} "
        f"skipped_unfetchable={stats['skipped_unfetchable']} to_fetch={stats['to_fetch']}"
    )
    if not args.write:
        print(f"# Dry run. Re-run with --write to fetch + cache {stats['fetched']} URN(s).")
    else:
        print(f"# done. ok={stats['ok']} miss={stats['miss']} err={stats['err']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

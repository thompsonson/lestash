#!/usr/bin/env python3
"""Resolve LinkedIn person URNs → human names from data already in the DB.

Two name sources are merged:
  1. `person_profiles` table — existing URN → display_name rows (sources:
     'manual', 'web', 'api', ...). Authoritative when present.
  2. `MemberAttributedEntity` annotations on comments — when someone
     @-mentions a person, the rendered name and URN are both in the
     metadata. Harvest gives URN → name pairs for free, no fetch.

Without --write: prints a report. The on-disk DB is not modified.
With    --write: upserts any newly-discovered pairs into `person_profiles`
                 with source='mention'. Existing rows are preserved
                 (COALESCE in upsert_person_profile).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

DB_PATH = Path.home() / ".config/lestash/lestash.db"


def walk_attributes(meta_raw: str) -> list[tuple[str, str]]:
    """Yield (urn, name) pairs from a metadata JSON blob.

    Looks at metadata.raw.activity.message: an `attributes` list of
    {start, length, value: {"com.linkedin.common.MemberAttributedEntity":
    {"member": URN}}}, and slices the name out of `text` using start+length.
    """
    out: list[tuple[str, str]] = []
    try:
        meta = json.loads(meta_raw)
    except (TypeError, json.JSONDecodeError):
        return out
    msg = (
        meta.get("raw", {})
        .get("activity", {})
        .get("message")
    )
    if not isinstance(msg, dict):  # ugcPosts have message as a plain string
        return out
    text = msg.get("text") or ""
    attrs = msg.get("attributes") or []
    if not text or not attrs:
        return out
    for a in attrs:
        try:
            start = int(a["start"])
            length = int(a["length"])
        except (KeyError, TypeError, ValueError):
            continue
        val = a.get("value") or {}
        entity = val.get("com.linkedin.common.MemberAttributedEntity") or {}
        urn = entity.get("member")
        if not urn or not isinstance(urn, str):
            continue
        if start < 0 or length <= 0 or start + length > len(text):
            continue
        name = text[start : start + length].strip()
        if name:
            out.append((urn, name))
    return out


def harvest_names_from_mentions(conn: sqlite3.Connection) -> dict[str, str]:
    """Collect URN→name across the whole DB from @-mentions. Most common name per URN."""
    name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    cursor = conn.execute(
        "SELECT metadata FROM items WHERE source_type='linkedin' AND metadata IS NOT NULL"
    )
    for (meta_raw,) in cursor:
        for urn, name in walk_attributes(meta_raw):
            name_counts[urn][name] += 1
    return {urn: counts.most_common(1)[0][0] for urn, counts in name_counts.items()}


def load_person_profiles(conn: sqlite3.Connection) -> dict[str, str]:
    """Read the existing URN → display_name map from person_profiles."""
    rows = conn.execute(
        "SELECT urn, display_name FROM person_profiles WHERE display_name IS NOT NULL"
    ).fetchall()
    return {urn: name for urn, name in rows}


def top_authors_with_names(
    conn: sqlite3.Connection, urn_to_name: dict[str, str], top_n: int = 25
) -> list[tuple[str, int, str | None]]:
    rows = conn.execute(
        """
        SELECT author, COUNT(*) AS n
        FROM items
        WHERE source_type='linkedin' AND author IS NOT NULL AND author != ''
        GROUP BY author
        ORDER BY n DESC
        """
    ).fetchall()
    return [(urn, n, urn_to_name.get(urn)) for urn, n in rows[:top_n]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--write",
        action="store_true",
        help="Upsert newly-discovered (URN, name) pairs into person_profiles "
        "with source='mention'. Existing rows are preserved.",
    )
    ap.add_argument("--top", type=int, default=30, help="Top-N person URNs to display")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        from_profiles = load_person_profiles(conn)
        from_mentions = harvest_names_from_mentions(conn)
        # person_profiles wins over @-mention disagreements (manual review > heuristic)
        urn_to_name: dict[str, str] = {**from_mentions, **from_profiles}

        new_from_mentions = {
            urn: name for urn, name in from_mentions.items() if urn not in from_profiles
        }
        print(f"# person_profiles rows loaded     : {len(from_profiles)}")
        print(f"# @-mention pairs harvested       : {len(from_mentions)}")
        print(f"# new pairs from mentions (no row): {len(new_from_mentions)}")
        print(f"# merged URN→name map size        : {len(urn_to_name)}\n")

        top = top_authors_with_names(conn, urn_to_name, top_n=args.top)
        print(f"## Top {args.top} person URNs by activity count in items.author")
        print(f"{'rank':>4}  {'count':>5}  urn                            name")
        for i, (urn, n, name) in enumerate(top, 1):
            print(f"{i:>4}  {n:>5}  {urn:<30} {name or '— unresolved —'}")

        unresolved = [(urn, n) for urn, n, name in top if not name]
        if unresolved:
            print(f"\n## Unresolved in the top {args.top}: {len(unresolved)}")
            print("Run linkedin_url_lookup.py against any post URLs you know they wrote.")

        if args.write:
            from lestash.core.database import upsert_person_profile
            written = 0
            for urn, name in new_from_mentions.items():
                upsert_person_profile(conn, urn, display_name=name, source="mention")
                written += 1
            print(f"\n## Wrote {written} new rows to person_profiles (source='mention')")
        else:
            print(f"\n## Dry run. Re-run with --write to upsert "
                  f"{len(new_from_mentions)} new pair(s) into person_profiles.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

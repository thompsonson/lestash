#!/usr/bin/env python3
"""Resolve LinkedIn person URNs → human names from data already in the DB.

LinkedIn comments use a `MemberAttributedEntity` annotation: when a commenter
@-mentions someone, the message text contains the rendered name and an
attribute records the mention range + the URN. That means every @-mention
across the DB is a URN→name pair we can harvest without any fetch.

This script walks every linkedin item, collects MemberAttributedEntity
annotations, and prints:
  1. A URN→name map
  2. The top N person URNs by activity count in `items.author`, resolved to
     names where possible
  3. Unresolved URNs (no name found anywhere) so you can decide if they're
     worth fetching one-by-one with linkedin_url_lookup.py

Read-only on the DB.
"""

from __future__ import annotations

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


def harvest_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Collect URN→name across the whole DB. Picks the most common name per URN."""
    name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    cursor = conn.execute(
        "SELECT metadata FROM items WHERE source_type='linkedin' AND metadata IS NOT NULL"
    )
    for (meta_raw,) in cursor:
        for urn, name in walk_attributes(meta_raw):
            name_counts[urn][name] += 1
    return {urn: counts.most_common(1)[0][0] for urn, counts in name_counts.items()}


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
    conn = sqlite3.connect(DB_PATH)
    try:
        urn_to_name = harvest_names(conn)
        print(f"# URN→name pairs harvested from @-mentions: {len(urn_to_name)}\n")

        top = top_authors_with_names(conn, urn_to_name, top_n=30)
        print("## Top 30 person URNs by activity count in items.author")
        print(f"{'rank':>4}  {'count':>5}  urn                            name")
        for i, (urn, n, name) in enumerate(top, 1):
            print(f"{i:>4}  {n:>5}  {urn:<30} {name or '— unresolved —'}")

        unresolved = [(urn, n) for urn, n, name in top if not name]
        if unresolved:
            print(f"\n## Unresolved in the top 30: {len(unresolved)}")
            print("Run linkedin_url_lookup.py against any post URLs you know they wrote.")

        print(f"\n## Sample of 20 URN→name pairs harvested:")
        for urn, name in list(urn_to_name.items())[:20]:
            print(f"  {urn:<30} {name}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

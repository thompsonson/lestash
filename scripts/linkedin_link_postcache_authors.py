#!/usr/bin/env python3
"""Cross-link post_cache.author rows with person_profiles, and apply a curated
handle → human-name map.

Three passes over each post_cache row where author_urn IS NULL:

  1. If author_name looks like a known LinkedIn handle from CURATED_HANDLES,
     upgrade it to the human name.
  2. Exact case-insensitive match: author_name == person_profiles.display_name
     → set author_urn from that row.
  3. Normalised handle match: when author_name is still a handle shape
     ("mike-schoonover"), compare against normalise(display_name) for every
     person_profile, and on a hit upgrade BOTH author_name → display_name
     AND author_urn.

Read-only without --write. With --write, runs the UPDATEs in one transaction.

Idempotent — re-running only touches rows where author_urn is still NULL.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from pathlib import Path

DB_PATH = Path.home() / ".config/lestash/lestash.db"

# Curated LinkedIn handle → human display name. Extend over time; the script
# does nothing destructive if a handle here is wrong — it only upgrades rows
# whose current author_name exactly equals the handle.
CURATED_HANDLES: dict[str, str] = {
    "gradybooch": "Grady Booch",
    "gary-marcus-b6384b4": "Gary Marcus",
    "hillel-wayne": "Hillel Wayne",
    "jasongorman": "Jason Gorman",
    "patrickdebois": "Patrick Debois",
    "johncrickett": "John Crickett",
    "yann-lecun": "Yann LeCun",
    "danielhanchen": "Daniel Hanchen",
    "nicholasbs": "Nicholas Bergson-Shilcock",
    "veit-heller": "Veit Heller",
    "cadrlife": "Ray Myers",
    "robnewby": "Rob Newby",
}

HANDLE_SHAPE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")  # lowercase, hyphens, no spaces


def normalise(s: str) -> str:
    """Diacritic-stripped, lowercase, spaces→hyphens. Used for handle ↔ name match."""
    no_accents = "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )
    return re.sub(r"\s+", "-", no_accents.lower().strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="Apply UPDATEs to post_cache.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        people = conn.execute(
            "SELECT urn, display_name FROM person_profiles WHERE display_name IS NOT NULL"
        ).fetchall()
        by_name_ci = {name.lower(): (urn, name) for urn, name in people}
        by_normalised = {normalise(name): (urn, name) for urn, name in people}

        rows = conn.execute(
            """
            SELECT urn, author_name FROM post_cache
            WHERE author_urn IS NULL AND author_name IS NOT NULL AND author_name != ''
            """
        ).fetchall()

        updates: list[tuple[str, str | None, str]] = []  # (new_name, new_urn, post_urn)
        handle_lifts = exact_hits = normalised_hits = 0

        for post_urn, author_name in rows:
            new_name = author_name
            new_urn: str | None = None
            reason = None

            if author_name in CURATED_HANDLES:
                new_name = CURATED_HANDLES[author_name]
                handle_lifts += 1
                reason = "curated"

            hit = by_name_ci.get(new_name.lower())
            if hit:
                new_urn, new_name = hit
                exact_hits += 1
                reason = (reason + "+exact") if reason else "exact"
            elif HANDLE_SHAPE_RE.match(new_name):
                hit = by_normalised.get(new_name)
                if hit:
                    new_urn, new_name = hit
                    normalised_hits += 1
                    reason = (reason + "+normalised") if reason else "normalised"

            if new_name != author_name or new_urn is not None:
                updates.append((new_name, new_urn, post_urn))
                print(f"  {reason:24s}  {author_name!r:38s} → {new_name!r}"
                      + (f"  [{new_urn}]" if new_urn else ""))

        print()
        print(f"# rows considered            : {len(rows)}")
        print(f"#   curated handle lifts     : {handle_lifts}")
        print(f"#   exact name matches       : {exact_hits}")
        print(f"#   normalised handle matches: {normalised_hits}")
        print(f"# rows to update             : {len(updates)}")

        if not args.write:
            print("\n# Dry run. Re-run with --write to apply.")
            return 0

        with conn:
            for new_name, new_urn, post_urn in updates:
                conn.execute(
                    "UPDATE post_cache SET author_name = ?, author_urn = COALESCE(?, author_urn) WHERE urn = ?",
                    (new_name, new_urn, post_urn),
                )
        print(f"\n# applied {len(updates)} UPDATE(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""One-shot: for existing items where commented_on / reacted_to is NULL but
the parent URN is encoded in raw.resourceUri, write the URN into metadata.

Companion to the extractor fix that adds the same fallback going forward.
Existing items synced before the fix don't get re-extracted automatically,
so this script repairs them in place.

Reads raw.resourceUri (format: /socialActions/<URN>/(comments|likes)/<id>),
extracts the URN, and updates the JSON column. Read-only without --write.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".config/lestash/lestash.db"

_RESOURCE_URI_PARENT_RE = re.compile(r"/socialActions/(urn:li:[^/]+)/(?:comments|likes)/")


def parent_urn_from_resource_uri(resource_uri: str | None) -> str | None:
    if not resource_uri:
        return None
    m = _RESOURCE_URI_PARENT_RE.search(resource_uri)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="Apply UPDATEs.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id,
                   metadata,
                   json_extract(metadata, '$.resource_name'),
                   json_extract(metadata, '$.raw.resourceUri')
            FROM items
            WHERE source_type='linkedin' AND is_own_content=1
              AND json_extract(metadata, '$.resource_name') IN
                    ('socialActions/comments', 'socialActions/likes')
              AND json_extract(metadata, '$.commented_on') IS NULL
              AND json_extract(metadata, '$.reacted_to') IS NULL
            """
        ).fetchall()

        updates: list[tuple[int, str]] = []  # (item_id, new_metadata_json)
        unrecoverable: list[tuple[int, str | None]] = []
        for item_id, meta_str, resource_name, resource_uri in rows:
            parent_urn = parent_urn_from_resource_uri(resource_uri)
            if not parent_urn:
                unrecoverable.append((item_id, resource_uri))
                continue
            meta = json.loads(meta_str)
            key = "commented_on" if resource_name == "socialActions/comments" else "reacted_to"
            meta[key] = parent_urn
            updates.append((item_id, json.dumps(meta)))

        print(f"# rows scanned          : {len(rows)}")
        print(f"# recoverable           : {len(updates)}")
        print(f"# no URN in resourceUri : {len(unrecoverable)}")
        if unrecoverable:
            print("  sample of unrecoverable:")
            for item_id, uri in unrecoverable[:5]:
                print(f"    item {item_id}: resourceUri={uri!r}")

        if not args.write:
            print("\n# Dry run. Re-run with --write to apply.")
            return 0

        with conn:
            for item_id, new_meta in updates:
                conn.execute(
                    "UPDATE items SET metadata = ? WHERE id = ?",
                    (new_meta, item_id),
                )
        print(f"\n# applied {len(updates)} UPDATE(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Related items: whole items from other sources whose vectors are close (#219).

See docs/content-and-activity-ontology-design.md, "Related items". Each scan compares
an item's vector with every other whole item's, keeps up to ``PER_ITEM`` from other
sources at ``MIN_SCORE`` or more, and stores them in ``item_links`` as ``related``,
``suggested``. A pair is stored once, with the lower id first.

- Confirming sets ``confirmed``; rejecting sets ``rejected``. Neither is touched again,
  and the row stops the pair from being suggested once more.
- A rescan replaces the suggestions that item's own scan made (``found_by`` in the
  evidence), so a changed item drops matches it no longer has.
- An item is scanned again when its vector changes: ``related_scans`` keeps the
  ``embedded_at`` and model of the vector each scan used.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from lestash.core.embeddings import VEC_TABLE, building, query_model

KIND = "related"
ORIGIN = "linker:vectors"
MANUAL = "manual"
# Cosine similarity. Chosen on 16 Nov 2026 from the live scores, cross-source: the best
# match per item has median 0.47 and 95th percentile 0.67; at 0.65 a sample read as
# related, at 0.60 it was visibly noisier (#219).
MIN_SCORE = 0.65
PER_ITEM = 5
# Very short texts ("this !") match each other on little more than their length
MIN_CHARS = 60
# Rows of the similarity matrix worked out at once: 512 x 20,000 floats is 40 MB
BLOCK = 512
# Candidates looked at per item before those already linked are dropped
LOOK_AT = PER_ITEM * 4

Decision = Literal["confirmed", "rejected"]


@dataclass
class RelateCounts:
    """What a scan did."""

    scanned: int = 0  # items whose suggestions were worked out again
    dropped: int = 0  # items no longer compared: gone, stubbed, emptied, moved under another
    suggested: int = 0  # suggested related pairs stored after the scan
    building: bool = False  # a new model's index is being built, so nothing was done
    seconds: float = 0.0


def _whole_items(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    """Each whole item compared: (id, source_type, vector, embedded_at, model)."""
    return conn.execute(
        f"""
        SELECT v.item_id, i.source_type, v.embedding, m.embedded_at, m.model
        FROM {VEC_TABLE} v
        JOIN items i ON i.id = v.item_id
        JOIN embedding_meta m ON m.item_id = v.item_id
        WHERE i.parent_id IS NULL AND i.held = 1
          AND length(coalesce(i.title, '')) + length(coalesce(i.content, '')) >= ?
        ORDER BY v.item_id
        """,
        (MIN_CHARS,),
    ).fetchall()


def _linked_pairs(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """Pairs a link of another kind joins already, in either direction."""
    return {
        (min(a, b), max(a, b))
        for a, b in conn.execute("SELECT from_id, to_id FROM item_links WHERE kind != ?", (KIND,))
    }


def _forget(conn: sqlite3.Connection, item_id: int) -> None:
    """Drop the suggestions an item's scan made, and those naming it at all when it is
    no longer compared. Decided rows stay."""
    conn.execute(
        "DELETE FROM item_links WHERE kind = ? AND status = 'suggested' "
        "AND (from_id = ? OR to_id = ?)",
        (KIND, item_id, item_id),
    )
    conn.execute("DELETE FROM related_scans WHERE item_id = ?", (item_id,))


def relate(conn: sqlite3.Connection, *, everything: bool = False) -> RelateCounts:
    """Suggest related items for every whole item whose vector changed since its last
    scan; with ``everything``, for every whole item. Commits as it goes.

    Needs the sqlite-vec extension loaded. Does nothing while a new model's index is
    built: the vectors search reads are about to be replaced.
    """
    started = time.monotonic()
    counts = RelateCounts()
    if building(conn):
        counts.building = True
        return counts
    model = query_model(conn)

    rows = _whole_items(conn)
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    position = {int(item_id): n for n, item_id in enumerate(ids)}
    scanned = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT item_id, embedded_at, model FROM related_scans")
    }

    gone = set(scanned) - set(position)
    for item_id in gone:
        _forget(conn, item_id)
    counts.dropped = len(gone)
    conn.commit()

    todo = [n for n, r in enumerate(rows) if everything or scanned.get(r[0]) != (str(r[3]), r[4])]
    if todo and len(rows) > 1:
        vectors = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(
            len(rows), -1
        )
        codes: dict[str, int] = {}
        source = np.array([codes.setdefault(r[1], len(codes)) for r in rows])
        linked = _linked_pairs(conn)
        look = min(LOOK_AT, len(rows) - 1)
        for start in range(0, len(todo), BLOCK):
            block = np.array(todo[start : start + BLOCK])
            scores = vectors[block] @ vectors.T
            # Only other sources: same-source neighbours (one channel's videos, one
            # account's posts) would drown the rest. An item's own source covers itself.
            scores[source[block, None] == source[None, :]] = -np.inf
            best = np.argpartition(-scores, look - 1, axis=1)[:, :look]
            best_scores = np.take_along_axis(scores, best, axis=1)
            order = np.argsort(-best_scores, axis=1)
            best = np.take_along_axis(best, order, axis=1)
            best_scores = np.take_along_axis(best_scores, order, axis=1)

            conn.execute("BEGIN IMMEDIATE")
            try:
                for row_n, n in enumerate(block):
                    item_id = int(ids[n])
                    conn.execute(
                        "DELETE FROM item_links WHERE kind = ? AND status = 'suggested' "
                        "AND json_extract(evidence, '$.found_by') = ?",
                        (KIND, item_id),
                    )
                    kept = 0
                    for other_n, score in zip(best[row_n], best_scores[row_n], strict=True):
                        if kept == PER_ITEM or score < MIN_SCORE:
                            break
                        other = int(ids[other_n])
                        pair = (min(item_id, other), max(item_id, other))
                        if pair in linked:
                            continue
                        evidence = json.dumps(
                            {"score": round(float(score), 3), "model": model, "found_by": item_id}
                        )
                        # A decided pair, or one the other item's scan found, is kept as it is
                        conn.execute(
                            "INSERT OR IGNORE INTO item_links "
                            "(from_id, to_id, kind, evidence, origin, status) "
                            "VALUES (?, ?, ?, ?, ?, 'suggested')",
                            (*pair, KIND, evidence, ORIGIN),
                        )
                        kept += 1
                    conn.execute(
                        "INSERT INTO related_scans (item_id, embedded_at, model) VALUES (?, ?, ?) "
                        "ON CONFLICT(item_id) DO UPDATE SET embedded_at = excluded.embedded_at, "
                        "model = excluded.model, scanned_at = CURRENT_TIMESTAMP",
                        (item_id, str(rows[n][3]), rows[n][4]),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            counts.scanned += len(block)

    counts.suggested = conn.execute(
        "SELECT COUNT(*) FROM item_links WHERE kind = ? AND status = 'suggested'", (KIND,)
    ).fetchone()[0]
    counts.seconds = time.monotonic() - started
    return counts


def suggestions_for(conn: sqlite3.Connection, item_id: int, limit: int = PER_ITEM) -> list[Any]:
    """An item's suggested related items, best first, as (item row, score). Pairs a
    link of another kind has joined since are left out."""
    return [
        (row, row["score"])
        for row in conn.execute(
            """
            SELECT i.*, json_extract(l.evidence, '$.score') AS score
            FROM item_links l
            JOIN items i ON i.id = CASE WHEN l.from_id = :id THEN l.to_id ELSE l.from_id END
            WHERE (l.from_id = :id OR l.to_id = :id) AND l.kind = :kind
              AND l.status = 'suggested' AND i.parent_id IS NULL AND i.held = 1
              AND NOT EXISTS (
                SELECT 1 FROM item_links o
                WHERE o.kind != :kind
                  AND ((o.from_id = l.from_id AND o.to_id = l.to_id)
                    OR (o.from_id = l.to_id AND o.to_id = l.from_id)))
            ORDER BY score DESC, i.id
            LIMIT :limit
            """,
            {"id": item_id, "limit": limit, "kind": KIND},
        )
    ]


def rejected_with(conn: sqlite3.Connection, item_id: int) -> set[int]:
    """The items whose relation to this one was rejected."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT CASE WHEN from_id = :id THEN to_id ELSE from_id END FROM item_links "
            "WHERE kind = :kind AND status = 'rejected' AND (from_id = :id OR to_id = :id)",
            {"id": item_id, "kind": KIND},
        )
    }


def decide(conn: sqlite3.Connection, item_id: int, other_id: int, status: Decision) -> None:
    """Confirm or reject that two items are related, suggested or not: a pair you
    found yourself, such as two items from one source, is stored as ``manual``. The
    time is kept in ``decided_at``. The caller commits."""
    if item_id == other_id:
        raise ValueError("An item can't be related to itself")
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"Not a decision: {status}")
    pair = (min(item_id, other_id), max(item_id, other_id))
    conn.execute(
        "INSERT INTO item_links (from_id, to_id, kind, evidence, origin, status, decided_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(from_id, to_id, kind) DO UPDATE SET status = excluded.status, "
        "decided_at = excluded.decided_at",
        (*pair, KIND, MANUAL, status),
    )

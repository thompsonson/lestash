"""Vector embeddings for semantic search using sqlite-vec and sentence-transformers."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import struct
import time
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlite_vec

from lestash.core.embed_units import ITEM_RECIPE, embed_source, embedding_units

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

    from lestash.models.item import Item

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# A top-level item's recipe; the others, and what gets a vector, are in embed_units.
# Each vector records its recipe, so changing one (or the model) re-embeds what it
# touches at the next refresh. Give a changed recipe a new name.
RECIPE = ITEM_RECIPE
BATCH_SIZE = 64

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """Lazy-load the sentence-transformers model."""
    global _model  # noqa: PLW0603
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info(f"Loading embedding model: {MODEL_NAME}")
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_text(text: str) -> list[float]:
    """Embed a single text string."""
    model = get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts."""
    model = get_model()
    embeddings = model.encode(texts, normalize_embeddings=True, batch_size=64)
    return [e.tolist() for e in embeddings]


def make_embed_text(item: Item) -> str:
    """Build the text string to embed for an item."""
    return embed_source(item.title, item.content)


def text_hash(text: str) -> str:
    """The hash a vector records of its text, to tell when the text has changed."""
    return hashlib.sha256(text.encode()).hexdigest()


def _serialize_embedding(embedding: list[float]) -> bytes:
    """Serialize a float list to bytes for sqlite-vec."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def load_vec_extension(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into a connection."""
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def ensure_vec_table(conn: sqlite3.Connection) -> None:
    """Create the vec_items virtual table if it doesn't exist."""
    load_vec_extension(conn)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
        f"item_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
    )
    conn.commit()


def upsert_embedding(
    conn: sqlite3.Connection,
    item_id: int,
    embedding: list[float],
    text: str,
    recipe: str = RECIPE,
) -> None:
    """Insert or replace an item's embedding, with the model, recipe and text behind it."""
    blob = _serialize_embedding(embedding)
    # vec0 tables don't support INSERT OR REPLACE on their primary key
    conn.execute("DELETE FROM vec_items WHERE item_id = ?", (item_id,))
    conn.execute("INSERT INTO vec_items (item_id, embedding) VALUES (?, ?)", (item_id, blob))
    conn.execute(
        "INSERT INTO embedding_meta (item_id, model, recipe, text_hash, embedded_at) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(item_id) DO UPDATE SET model = excluded.model, "
        "recipe = excluded.recipe, text_hash = excluded.text_hash, "
        "embedded_at = excluded.embedded_at",
        (item_id, MODEL_NAME, recipe, text_hash(text)),
    )


def delete_embedding(conn: sqlite3.Connection, item_id: int) -> None:
    """Remove an embedding and its record."""
    conn.execute("DELETE FROM vec_items WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM embedding_meta WHERE item_id = ?", (item_id,))


def search_similar(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    limit: int = 20,
) -> list[tuple[int, float]]:
    """Find items similar to a query embedding via KNN.

    Returns list of (item_id, distance) tuples, lowest distance first.
    """
    blob = _serialize_embedding(query_embedding)
    rows = conn.execute(
        "SELECT item_id, distance FROM vec_items WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (blob, limit),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def re_embed_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Bring the vectors of an item's family up to date after it changed: the item,
    its parent or its children, whose texts may include its own.

    Returns True if the item has a vector afterwards. Errors are logged but never
    raised, so callers (PATCH, restore) can continue without surfacing infra failures.
    """
    row = conn.execute("SELECT parent_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return False
    family = row[0] if row[0] is not None else item_id
    try:
        load_vec_extension(conn)
        ensure_vec_table(conn)
        refresh_embeddings(conn, families={family})
        return (
            conn.execute("SELECT 1 FROM embedding_meta WHERE item_id = ?", (item_id,)).fetchone()
            is not None
        )
    except Exception:
        logger.warning("Re-embed failed for item %d", item_id, exc_info=True)
        return False


def get_embedding_stats(conn: sqlite3.Connection) -> dict:
    """Get embedding coverage statistics."""
    total_parents = conn.execute(
        "SELECT COUNT(*) FROM items WHERE parent_id IS NULL AND held = 1"
    ).fetchone()[0]

    # Vectors of top-level items, and of their parts (rounds, comments, highlights...)
    embedded, parts = conn.execute(
        "SELECT COALESCE(SUM(i.parent_id IS NULL), 0), COALESCE(SUM(i.parent_id IS NOT NULL), 0)"
        " FROM embedding_meta m JOIN items i ON i.id = m.item_id"
    ).fetchone()
    # Without a vector of today's model and recipe; changed text is found by a refresh
    out_of_date = conn.execute(
        "SELECT COUNT(*) FROM items i WHERE i.parent_id IS NULL AND i.held = 1 "
        "AND NOT EXISTS (SELECT 1 FROM embedding_meta m WHERE m.item_id = i.id "
        "AND m.model = ? AND m.recipe = ?)",
        (MODEL_NAME, RECIPE),
    ).fetchone()[0]

    return {
        "model": MODEL_NAME,
        "recipe": RECIPE,
        "dimensions": EMBEDDING_DIM,
        "embedded": embedded,
        "parts_embedded": parts,
        "out_of_date": out_of_date,
        "total_parents": total_parents,
        "coverage": f"{embedded / total_parents * 100:.1f}%" if total_parents else "0%",
    }


@dataclass
class RefreshCounts:
    """What a refresh did, and what it left for the next one."""

    embedded: int = 0
    removed: int = 0
    pending: int = 0  # still to embed: the budget ran out
    stopped: bool = False


def refresh_embeddings(
    conn: sqlite3.Connection,
    *,
    families: set[int] | None = None,
    budget_s: float | None = None,
    progress_callback: typing.Callable[[int, int], None] | None = None,
) -> RefreshCounts:
    """Bring the vectors up to date with the items (#221).

    - Embeds every unit (embed_units) with no vector, or whose vector came from another
      model, recipe or text. Newest items first.
    - Removes vectors that no unit stands for: items gone, stubbed, emptied, or no
      longer the start of a round.
    - With families, only those top-level items and their children.
    - Commits each batch. With a budget, stops after the batch that passes it; the
      next refresh goes on.

    An item edited while this runs may get a vector of its older text; its hash then
    differs, so the next refresh embeds it again.
    """
    ensure_vec_table(conn)
    counts = RefreshCounts()
    started = time.monotonic()

    if families is None:
        have_vector = {row[0] for row in conn.execute("SELECT item_id FROM vec_items")}
        recorded = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT item_id, model, recipe, text_hash FROM embedding_meta")
        }
    else:
        fams = list(families)
        marks = ",".join("?" * len(fams))
        members = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM items WHERE id IN ({marks}) OR parent_id IN ({marks})",
                fams + fams,
            )
        ]
        have_vector, recorded = set(), {}
        for start in range(0, len(members), 500):
            ids = members[start : start + 500]
            marks = ",".join("?" * len(ids))
            have_vector |= {
                row[0]
                for row in conn.execute(
                    f"SELECT item_id FROM vec_items WHERE item_id IN ({marks})", ids
                )
            }
            recorded.update(
                (row[0], (row[1], row[2], row[3]))
                for row in conn.execute(
                    "SELECT item_id, model, recipe, text_hash FROM embedding_meta"
                    f" WHERE item_id IN ({marks})",
                    ids,
                )
            )

    keep: set[int] = set()
    todo: list[tuple[int, str, str]] = []
    for unit in embedding_units(conn, families):
        keep.add(unit.item_id)
        current = (MODEL_NAME, unit.recipe, text_hash(unit.text))
        if unit.item_id not in have_vector or recorded.get(unit.item_id) != current:
            todo.append((unit.item_id, unit.recipe, unit.text))
    todo.sort(key=lambda entry: entry[0], reverse=True)

    # Vectors, and records, that no unit stands for
    for item_id in (have_vector | set(recorded)) - keep:
        delete_embedding(conn, item_id)
        counts.removed += 1
    conn.commit()

    done = 0
    for start in range(0, len(todo), BATCH_SIZE):
        if budget_s is not None and time.monotonic() - started > budget_s:
            counts.stopped = True
            break
        batch = todo[start : start + BATCH_SIZE]
        vectors = embed_batch([text for _, _, text in batch])
        # Take the write lock before looking: an item deleted after the list was read
        # would break embedding_meta's foreign key. Its vector is dropped next time.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [item_id for item_id, _, _ in batch]
            marks = ",".join("?" * len(ids))
            present = {
                row[0] for row in conn.execute(f"SELECT id FROM items WHERE id IN ({marks})", ids)
            }
            for (item_id, recipe, text), vector in zip(batch, vectors, strict=True):
                if item_id in present:
                    upsert_embedding(conn, item_id, vector, text, recipe)
                    counts.embedded += 1
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        done += len(batch)
        if progress_callback:
            progress_callback(done, len(todo))

    counts.pending = len(todo) - done
    return counts


def rebuild_embeddings(
    conn: sqlite3.Connection,
    progress_callback: typing.Callable[[int, int], None] | None = None,
) -> int:
    """Embed every item that is new or changed. Returns the number embedded."""
    return refresh_embeddings(conn, progress_callback=progress_callback).embedded

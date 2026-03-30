"""Vector embeddings for semantic search using sqlite-vec and sentence-transformers."""

from __future__ import annotations

import logging
import sqlite3
import struct
import typing
from typing import TYPE_CHECKING

import sqlite_vec

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

    from lestash.models.item import Item

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

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
    parts = []
    if item.title:
        parts.append(item.title)
    if item.content:
        parts.append(item.content[:500])
    return " ".join(parts) or ""


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


def upsert_embedding(conn: sqlite3.Connection, item_id: int, embedding: list[float]) -> None:
    """Insert or replace an embedding for an item."""
    blob = _serialize_embedding(embedding)
    conn.execute(
        "INSERT OR REPLACE INTO vec_items (item_id, embedding) VALUES (?, ?)",
        (item_id, blob),
    )


def delete_embedding(conn: sqlite3.Connection, item_id: int) -> None:
    """Remove an embedding."""
    conn.execute("DELETE FROM vec_items WHERE item_id = ?", (item_id,))


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


def get_embedding_stats(conn: sqlite3.Connection) -> dict:
    """Get embedding coverage statistics."""
    total_parents = conn.execute("SELECT COUNT(*) FROM items WHERE parent_id IS NULL").fetchone()[0]

    try:
        embedded = conn.execute("SELECT COUNT(*) FROM vec_items").fetchone()[0]
    except sqlite3.OperationalError:
        embedded = 0

    return {
        "model": MODEL_NAME,
        "dimensions": EMBEDDING_DIM,
        "embedded": embedded,
        "total_parents": total_parents,
        "coverage": f"{embedded / total_parents * 100:.1f}%" if total_parents else "0%",
    }


def rebuild_embeddings(
    conn: sqlite3.Connection,
    progress_callback: typing.Callable[[int, int], None] | None = None,
) -> int:
    """Embed all parent items missing vectors. Returns count of items embedded."""
    ensure_vec_table(conn)

    # Find parent items without embeddings
    rows = conn.execute(
        """
        SELECT items.id, items.title, items.content
        FROM items
        LEFT JOIN vec_items ON items.id = vec_items.item_id
        WHERE items.parent_id IS NULL AND vec_items.item_id IS NULL
        """
    ).fetchall()

    if not rows:
        return 0

    # Batch embed
    texts = []
    item_ids = []
    for row in rows:
        title = row[1] or ""
        content = row[2] or ""
        text = f"{title} {content[:500]}".strip()
        if text:
            texts.append(text)
            item_ids.append(row[0])

    if not texts:
        return 0

    # Process in chunks to show progress
    chunk_size = 64
    total_embedded = 0
    for i in range(0, len(texts), chunk_size):
        chunk_texts = texts[i : i + chunk_size]
        chunk_ids = item_ids[i : i + chunk_size]
        embeddings = embed_batch(chunk_texts)

        for item_id, emb in zip(chunk_ids, embeddings, strict=True):
            upsert_embedding(conn, item_id, emb)

        total_embedded += len(chunk_texts)
        conn.commit()

        if progress_callback:
            progress_callback(total_embedded, len(texts))

    return total_embedded

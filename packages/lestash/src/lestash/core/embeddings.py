"""Vector embeddings for semantic search using sqlite-vec and sentence-transformers."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import struct
import time
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlite_vec

from lestash.core.embed_units import (
    ITEM_RECIPE,
    TITLE_ONLY,
    TITLE_ONLY_SLACK,
    embed_source,
    embedding_units,
)

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

    from lestash.models.item import Item

logger = logging.getLogger(__name__)

# Chosen from the experiments in docs/search-experiments.md (#305). Gated on Hugging
# Face under Google's Gemma terms: the machine's token must have accepted them.
MODEL_NAME = "google/embeddinggemma-300m"
EMBEDDING_DIM = 768
MAX_SEQ_LENGTH = 512  # tokens; the units are far shorter (E4 may change both)
ENCODE_BATCH = 32

# The index search reads. A change of model builds the new one beside it, in
# NEXT_TABLE, and swaps it in once complete; until then queries use the model that
# made the old index, known by its width.
VEC_TABLE = "vec_items"
NEXT_TABLE = "vec_items_next"
LOG_SOURCE = "embeddings"  # rebuilds' rows in sync_log, shown in Settings → Sources
EARLIER_MODELS = {384: "all-MiniLM-L6-v2"}

# A top-level item's recipe; the others, and what gets a vector, are in embed_units.
# Each vector records its recipe, so changing one (or the model) re-embeds what it
# touches at the next refresh. Give a changed recipe a new name.
RECIPE = ITEM_RECIPE
BATCH_SIZE = 64

_models: dict[str, SentenceTransformer] = {}


def get_model(name: str = MODEL_NAME) -> SentenceTransformer:
    """Lazy-load a sentence-transformers model, once per process."""
    if name not in _models:
        from sentence_transformers import SentenceTransformer

        logger.info(f"Loading embedding model: {name}")
        model = SentenceTransformer(name)
        if name == MODEL_NAME:
            model.max_seq_length = MAX_SEQ_LENGTH
        _models[name] = model
    return _models[name]


def embed_text(text: str) -> list[float]:
    """Embed a search query, with the model's query prompt."""
    import numpy as np

    model = get_model()
    return np.asarray(model.encode_query(text, normalize_embeddings=True)).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed units, with the model's document prompt."""
    import numpy as np

    model = get_model()
    vectors = model.encode_document(texts, normalize_embeddings=True, batch_size=ENCODE_BATCH)
    return np.asarray(vectors).tolist()


def _table_dim(conn: sqlite3.Connection, table: str) -> int | None:
    """A vector table's width, or None if it doesn't exist."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    if not row:
        return None
    found = re.search(r"float\[(\d+)\]", row[0] or "")
    return int(found.group(1)) if found else None


def building(conn: sqlite3.Connection) -> bool:
    """Whether the index search reads is from an earlier model, the new one being built."""
    dim = _table_dim(conn, VEC_TABLE)
    return dim is not None and dim != EMBEDDING_DIM


def query_model(conn: sqlite3.Connection) -> str:
    """The model that made the index search reads."""
    dim = _table_dim(conn, VEC_TABLE)
    if dim is None or dim == EMBEDDING_DIM:
        return MODEL_NAME
    if dim not in EARLIER_MODELS:
        raise RuntimeError(f"{VEC_TABLE} holds {dim}-wide vectors of no known model")
    return EARLIER_MODELS[dim]


def embed_query(conn: sqlite3.Connection, text: str) -> list[float]:
    """Embed a search query for the index search reads: with an earlier model while
    the new index is built."""
    name = query_model(conn)
    if name == MODEL_NAME:
        return embed_text(text)
    return get_model(name).encode(text, normalize_embeddings=True).tolist()


def _write_table(conn: sqlite3.Connection) -> str:
    """Where new vectors go: the index, or the one being built beside it."""
    return NEXT_TABLE if building(conn) else VEC_TABLE


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


def _create(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
        f"item_id INTEGER PRIMARY KEY, embedding float[{EMBEDDING_DIM}])"
    )


def ensure_vec_table(conn: sqlite3.Connection) -> None:
    """Create the vector index if it doesn't exist; beside an earlier model's index,
    the one to build."""
    load_vec_extension(conn)
    _create(conn, VEC_TABLE)
    if building(conn):
        _create(conn, NEXT_TABLE)
    conn.commit()


def swap_in_next(conn: sqlite3.Connection) -> int:
    """Replace the earlier model's index with the one built beside it. Returns the
    number of vectors in it. Search reads the old index until this commits."""
    if not building(conn):
        return 0
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"DROP TABLE {VEC_TABLE}")
        _create(conn, VEC_TABLE)
        conn.execute(
            f"INSERT INTO {VEC_TABLE} (item_id, embedding) SELECT item_id, embedding"
            f" FROM {NEXT_TABLE}"
        )
        conn.execute(f"DROP TABLE {NEXT_TABLE}")
        count = conn.execute(f"SELECT COUNT(*) FROM {VEC_TABLE}").fetchone()[0]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("Swapped in the %s index: %d vectors", MODEL_NAME, count)
    return count


def upsert_embedding(
    conn: sqlite3.Connection,
    item_id: int,
    embedding: list[float],
    text: str,
    recipe: str = RECIPE,
) -> None:
    """Insert or replace an item's embedding, with the model, recipe and text behind it."""
    blob = _serialize_embedding(embedding)
    table = _write_table(conn)
    # vec0 tables don't support INSERT OR REPLACE on their primary key
    conn.execute(f"DELETE FROM {table} WHERE item_id = ?", (item_id,))
    conn.execute(f"INSERT INTO {table} (item_id, embedding) VALUES (?, ?)", (item_id, blob))
    conn.execute(
        "INSERT INTO embedding_meta (item_id, model, recipe, text_hash, embedded_at) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(item_id) DO UPDATE SET model = excluded.model, "
        "recipe = excluded.recipe, text_hash = excluded.text_hash, "
        "embedded_at = excluded.embedded_at",
        (item_id, MODEL_NAME, recipe, text_hash(text)),
    )


def delete_embedding(conn: sqlite3.Connection, item_id: int) -> None:
    """Remove an embedding and its record, from both indexes while one is built."""
    conn.execute(f"DELETE FROM {VEC_TABLE} WHERE item_id = ?", (item_id,))
    if building(conn):
        conn.execute(f"DELETE FROM {NEXT_TABLE} WHERE item_id = ?", (item_id,))
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
    # Top-level items that should have a vector: held, with text, not watch history
    wanted = (
        "i.parent_id IS NULL AND i.held = 1"
        " AND (coalesce(i.title, '') != '' OR coalesce(i.content, '') != '')"
        f" AND NOT {TITLE_ONLY.format(t='i')}"
    )
    total_parents = conn.execute(
        f"SELECT COUNT(*) FROM items i WHERE {wanted}", (TITLE_ONLY_SLACK,)
    ).fetchone()[0]

    # This model's vectors of top-level items, and of their parts (rounds, comments...)
    embedded, parts = conn.execute(
        "SELECT COALESCE(SUM(i.parent_id IS NULL), 0), COALESCE(SUM(i.parent_id IS NOT NULL), 0)"
        " FROM embedding_meta m JOIN items i ON i.id = m.item_id WHERE m.model = ?",
        (MODEL_NAME,),
    ).fetchone()
    # Without a vector of today's model and recipe; changed text is found by a refresh
    out_of_date = conn.execute(
        f"SELECT COUNT(*) FROM items i WHERE {wanted} "
        "AND NOT EXISTS (SELECT 1 FROM embedding_meta m WHERE m.item_id = i.id "
        "AND m.model = ? AND m.recipe = ?)",
        (TITLE_ONLY_SLACK, MODEL_NAME, RECIPE),
    ).fetchone()[0]
    is_building = building(conn)

    return {
        "model": MODEL_NAME,
        "recipe": RECIPE,
        "dimensions": EMBEDDING_DIM,
        # The model search uses now: an earlier one while the new index is built
        "query_model": query_model(conn),
        "building": is_building,
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
    swapped: int = 0  # vectors in the new model's index, if this refresh swapped it in
    seconds: float = 0.0


def plan_refresh(
    conn: sqlite3.Connection, families: set[int] | None = None
) -> tuple[list[tuple[int, str, str]], int]:
    """What a refresh must embed, newest first, as (item_id, recipe, text); and drop
    the vectors no unit stands for. Returns the list and the number dropped."""
    ensure_vec_table(conn)
    table = _write_table(conn)

    if families is None:
        have_vector = {row[0] for row in conn.execute(f"SELECT item_id FROM {table}")}
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
                    f"SELECT item_id FROM {table} WHERE item_id IN ({marks})", ids
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

    # Vectors, and records, that no unit stands for. While a new index is built, the
    # old one's vectors of units now gone go too: search would still return them.
    gone = have_vector | set(recorded)
    if table == NEXT_TABLE and families is None:
        gone |= {row[0] for row in conn.execute(f"SELECT item_id FROM {VEC_TABLE}")}
    removed = 0
    for item_id in gone - keep:
        delete_embedding(conn, item_id)
        removed += 1
    conn.commit()
    return todo, removed


def store_vectors(
    conn: sqlite3.Connection, batch: list[tuple[int, str, str]], vectors: list[list[float]]
) -> int:
    """Write one batch of vectors and their records. Returns the number written."""
    # Take the write lock before looking: an item deleted after the list was read
    # would break embedding_meta's foreign key. Its vector is dropped next time.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    written = 0
    try:
        ids = [item_id for item_id, _, _ in batch]
        marks = ",".join("?" * len(ids))
        present = {
            row[0] for row in conn.execute(f"SELECT id FROM items WHERE id IN ({marks})", ids)
        }
        for (item_id, recipe, text), vector in zip(batch, vectors, strict=True):
            if len(vector) != EMBEDDING_DIM:
                raise ValueError(f"a {len(vector)}-wide vector, not {EMBEDDING_DIM}")
            if item_id in present:
                upsert_embedding(conn, item_id, vector, text, recipe)
                written += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return written


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
    - After a change of model, builds the new index beside the old one; a whole
      refresh that finishes swaps it in (#305).

    An item edited while this runs may get a vector of its older text; its hash then
    differs, so the next refresh embeds it again.
    """
    started = time.monotonic()
    todo, removed = plan_refresh(conn, families)
    counts = RefreshCounts(removed=removed)

    done = 0
    for start in range(0, len(todo), BATCH_SIZE):
        if budget_s is not None and time.monotonic() - started > budget_s:
            counts.stopped = True
            break
        batch = todo[start : start + BATCH_SIZE]
        vectors = embed_batch([text for _, _, text in batch])
        counts.embedded += store_vectors(conn, batch, vectors)
        done += len(batch)
        if progress_callback:
            progress_callback(done, len(todo))

    counts.pending = len(todo) - done
    if families is None and not counts.pending:
        counts.swapped = swap_in_next(conn)
    counts.seconds = time.monotonic() - started
    return counts


def rebuild_embeddings(
    conn: sqlite3.Connection,
    progress_callback: typing.Callable[[int, int], None] | None = None,
) -> int:
    """Embed every item that is new or changed. Returns the number embedded."""
    return refresh_embeddings(conn, progress_callback=progress_callback).embedded

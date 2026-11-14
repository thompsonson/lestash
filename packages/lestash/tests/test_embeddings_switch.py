"""The switch to EmbeddingGemma (#305): watch-history videos without vectors, the new
index built beside the old one and swapped in, and rebuilds on another machine."""

import hashlib
import json
import struct
from pathlib import Path

import lestash.core.embeddings as emb
import pytest
from lestash.core import embed_remote, embed_worker
from lestash.core import embed_units as units
from lestash.core.database import get_connection

OLD_DIM = 384


def _fake_vector(text: str, dim: int = emb.EMBEDDING_DIM) -> list[float]:
    seed = hashlib.sha256(text.encode()).digest()
    vector = [b / 255 + 0.01 for b in (seed * 32)[:dim]]
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector]


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(emb, "embed_batch", lambda texts: [_fake_vector(t) for t in texts])
    monkeypatch.setattr(emb, "embed_text", _fake_vector)


@pytest.fixture
def conn(test_db):
    with get_connection(test_db) as conn:
        emb.load_vec_extension(conn)
        yield conn


def _add(conn, source_id, title, content, *, source="note", parent_id=None):
    """An item; from YouTube, its kind is video."""
    item_id = conn.execute(
        "INSERT INTO items (source_type, source_id, title, content, parent_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, source_id, title, content, parent_id),
    ).lastrowid
    conn.commit()
    return item_id


def _old_index(conn, *item_ids):
    """An index made by the earlier model, as on pop-mini before the switch."""
    conn.execute(
        "CREATE VIRTUAL TABLE vec_items USING vec0("
        f"item_id INTEGER PRIMARY KEY, embedding float[{OLD_DIM}])"
    )
    for item_id in item_ids:
        blob = struct.pack(f"{OLD_DIM}f", *_fake_vector(str(item_id), OLD_DIM))
        conn.execute("INSERT INTO vec_items (item_id, embedding) VALUES (?, ?)", (item_id, blob))
        conn.execute(
            "INSERT INTO embedding_meta (item_id, model, recipe, text_hash) VALUES (?, ?, ?, ?)",
            (item_id, "all-MiniLM-L6-v2", "title+content500", "old"),
        )
    conn.commit()


def _ids(conn, table):
    return {row[0] for row in conn.execute(f"SELECT item_id FROM {table}")}


def _exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone()


# What gets a vector


def test_a_video_with_only_its_title_gets_no_vector(conn):
    watched = _add(conn, "w", "A talk on priors", "A talk on priors", source="youtube")
    described = _add(
        conn, "d", "A talk", "A talk. " + "It covers priors and posteriors. " * 5, source="youtube"
    )
    note = _add(conn, "n", "Short", "")

    found = {unit.item_id for unit in units.embedding_units(conn)}

    assert watched not in found
    assert {described, note} <= found


def test_a_title_only_video_inside_another_item_gets_no_vector(conn):
    playlist = _add(conn, "p", "A playlist", "Talks I liked")
    watched = _add(conn, "w", "A talk", "", source="youtube", parent_id=playlist)

    assert watched not in {unit.item_id for unit in units.embedding_units(conn)}


def test_stats_do_not_count_title_only_videos_as_waiting(conn):
    _add(conn, "w", "A talk on priors", "", source="youtube")
    _add(conn, "n", "A note", "Its text")

    emb.refresh_embeddings(conn)
    stats = emb.get_embedding_stats(conn)

    assert (stats["total_parents"], stats["embedded"], stats["out_of_date"]) == (1, 1, 0)


# The new index beside the old one


def test_a_new_database_reads_and_writes_the_new_index(conn):
    emb.ensure_vec_table(conn)

    assert not emb.building(conn)
    assert emb.query_model(conn) == emb.MODEL_NAME
    assert not _exists(conn, emb.NEXT_TABLE)


def test_beside_an_old_index_the_new_one_is_built_and_search_keeps_the_old(conn):
    post = _add(conn, "a", "A post", "Its text")
    _old_index(conn, post)

    emb.ensure_vec_table(conn)

    assert emb.building(conn)
    assert emb.query_model(conn) == "all-MiniLM-L6-v2"
    assert _exists(conn, emb.NEXT_TABLE)
    assert emb.get_embedding_stats(conn)["building"] is True


def test_queries_use_the_old_model_until_the_swap(conn, monkeypatch):
    _old_index(conn)
    asked = []

    class Old:
        def encode(self, text, normalize_embeddings):
            asked.append(text)
            return _fake_vector_array(text, OLD_DIM)

    monkeypatch.setattr(emb, "get_model", lambda name=emb.MODEL_NAME: Old())

    assert len(emb.embed_query(conn, "priors")) == OLD_DIM
    assert asked == ["priors"]


def _fake_vector_array(text, dim):
    import numpy as np

    return np.array(_fake_vector(text, dim))


def test_a_budgeted_refresh_builds_without_swapping(conn, monkeypatch):
    monkeypatch.setattr(emb, "BATCH_SIZE", 1)
    first = _add(conn, "a", "One", "text")
    second = _add(conn, "b", "Two", "text")
    _old_index(conn, first, second)

    counts = emb.refresh_embeddings(conn, budget_s=-1)

    assert counts.stopped and not counts.swapped
    assert emb.building(conn)
    assert _ids(conn, "vec_items") == {first, second}


def test_a_family_refresh_writes_the_new_index_and_never_swaps(conn):
    post = _add(conn, "a", "A post", "Its text")
    _old_index(conn, post)

    emb.refresh_embeddings(conn, families={post})

    assert emb.building(conn)
    assert _ids(conn, emb.NEXT_TABLE) == {post}
    assert _ids(conn, "vec_items") == {post}  # the old vector, still searched


def test_a_whole_refresh_swaps_the_new_index_in(conn):
    post = _add(conn, "a", "A post", "Its text")
    watched = _add(conn, "w", "A talk", "A talk", source="youtube")
    _old_index(conn, post, watched)

    counts = emb.refresh_embeddings(conn)

    assert counts.swapped == 1
    assert not emb.building(conn)
    assert not _exists(conn, emb.NEXT_TABLE)
    assert _ids(conn, "vec_items") == {post}
    assert emb.query_model(conn) == emb.MODEL_NAME
    model, recipe = conn.execute(
        "SELECT model, recipe FROM embedding_meta WHERE item_id = ?", (post,)
    ).fetchone()
    assert (model, recipe) == (emb.MODEL_NAME, units.ITEM_RECIPE)
    # Searchable with a query of the new width
    assert emb.search_similar(conn, _fake_vector("A post Its text"), limit=1)[0][0] == post


def test_old_vectors_of_units_now_gone_leave_search_at_once(conn, monkeypatch):
    post = _add(conn, "a", "A post", "Its text")
    watched = _add(conn, "w", "A talk", "A talk", source="youtube")
    _old_index(conn, post, watched)

    emb.refresh_embeddings(conn, budget_s=-1)  # plans and removes, embeds nothing

    assert _ids(conn, "vec_items") == {post}


def test_deleting_a_vector_removes_it_from_both_indexes(conn):
    post = _add(conn, "a", "A post", "Its text")
    _old_index(conn, post)
    emb.refresh_embeddings(conn, families={post})

    emb.delete_embedding(conn, post)

    assert _ids(conn, "vec_items") == _ids(conn, emb.NEXT_TABLE) == set()


def test_a_vector_of_the_wrong_width_is_refused(conn):
    post = _add(conn, "a", "A post", "Its text")
    emb.ensure_vec_table(conn)

    with pytest.raises(ValueError, match="384-wide"):
        emb.store_vectors(conn, [(post, units.ITEM_RECIPE, "x")], [[0.0] * OLD_DIM])
    assert _ids(conn, "vec_items") == set()


# A rebuild on another machine


def test_the_worker_files_round_trip(tmp_path):
    embed_worker.write_units(tmp_path, [(1, "one"), (2, "two")])
    assert embed_worker.read_units(tmp_path) == ["one", "two"]

    (tmp_path / "out").mkdir()
    vectors = [_fake_vector("one", 4), _fake_vector("two", 4)]
    (tmp_path / "out" / "vectors.f32").write_bytes(
        b"".join(struct.pack("<4f", *v) for v in vectors)
    )
    (tmp_path / "out" / "done.json").write_text(json.dumps({"count": 2, "dim": 4}))

    back = embed_worker.read_vectors(tmp_path, 2)
    assert [[round(x, 5) for x in v] for v in back] == [[round(x, 5) for x in v] for v in vectors]
    with pytest.raises(ValueError, match="2 vectors for 3 units"):
        embed_worker.read_vectors(tmp_path, 3)


def test_a_malformed_done_file_reads_as_a_value_error(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "vectors.f32").write_bytes(b"")
    for text in ("{}", "[1]", '{"count": "x", "dim": 4}', "not json"):
        (tmp_path / "out" / "done.json").write_text(text)
        with pytest.raises(ValueError, match="done.json can't be read"):
            embed_worker.read_vectors(tmp_path, 1)


class _FakeHost:
    def __init__(self, host):
        self.host = host

    def ssh(self, command, timeout=120):
        return "/home/someone"

    def copy(self, *args, timeout=600):
        pass


def test_a_rebuild_on_a_host_stores_its_vectors_and_swaps(conn, monkeypatch, tmp_path):
    first = _add(conn, "a", "One", "text")
    second = _add(conn, "b", "Two", "text")
    _old_index(conn, first)
    sent = []

    def run_batch(remote, root, batch, threads):
        sent.append(batch)
        return [_fake_vector(text) for _, text in batch]

    monkeypatch.setattr(embed_remote, "_Host", _FakeHost)
    monkeypatch.setattr(embed_remote, "_run_batch", run_batch)
    monkeypatch.setattr(embed_remote, "model_folder", lambda: tmp_path)
    monkeypatch.setattr(embed_remote, "CHUNK", 1)

    counts = embed_remote.rebuild_on(conn, "computer")

    assert [len(batch) for batch in sent] == [1, 1]
    assert (counts.embedded, counts.pending, counts.swapped) == (2, 0, 2)
    assert _ids(conn, "vec_items") == {first, second}
    assert not emb.building(conn)


def test_an_unreachable_host_raises_before_anything_changes(conn, monkeypatch, tmp_path):
    post = _add(conn, "a", "One", "text")
    _old_index(conn, post)

    class Down(_FakeHost):
        def ssh(self, command, timeout=120):
            import subprocess

            raise subprocess.CalledProcessError(255, "ssh", stderr="No route to host")

    monkeypatch.setattr(embed_remote, "_Host", Down)
    monkeypatch.setattr(embed_remote, "model_folder", lambda: Path(tmp_path))

    with pytest.raises(embed_remote.HostUnavailable, match="No route to host"):
        embed_remote.rebuild_on(conn, "computer")
    assert _ids(conn, "vec_items") == {post}
    assert not _exists(conn, emb.NEXT_TABLE)


def test_a_batch_that_fails_keeps_the_batches_back_and_raises(conn, monkeypatch, tmp_path):
    first = _add(conn, "a", "One", "text")
    second = _add(conn, "b", "Two", "text")
    _old_index(conn, first)
    calls = []

    def run_batch(remote, root, batch, threads):
        calls.append(batch)
        if len(calls) == 2:
            raise RuntimeError("embedding on computer stopped: Killed")
        return [_fake_vector(text) for _, text in batch]

    monkeypatch.setattr(embed_remote, "_Host", _FakeHost)
    monkeypatch.setattr(embed_remote, "_run_batch", run_batch)
    monkeypatch.setattr(embed_remote, "model_folder", lambda: tmp_path)
    monkeypatch.setattr(embed_remote, "CHUNK", 1)

    with pytest.raises(embed_remote.HostUnavailable, match="stopped after 1 of 2") as failed:
        embed_remote.rebuild_on(conn, "computer")
    assert (failed.value.counts.embedded, failed.value.counts.pending) == (1, 1)
    assert _ids(conn, emb.NEXT_TABLE) == {second}  # newest first
    # The next run goes on from the batch kept, and swaps
    assert emb.refresh_embeddings(conn).swapped == 2
    assert first in _ids(conn, "vec_items")


def test_a_model_that_cannot_be_fetched_counts_as_the_host_unavailable(conn, monkeypatch):
    def no_model():
        raise OSError("gated repo")

    monkeypatch.setattr(embed_remote, "_Host", _FakeHost)
    monkeypatch.setattr(embed_remote, "model_folder", no_model)

    with pytest.raises(embed_remote.HostUnavailable, match="gated repo"):
        embed_remote.rebuild_on(conn, "computer")


def test_a_host_name_that_reads_as_an_option_is_refused():
    with pytest.raises(embed_remote.HostUnavailable):
        embed_remote._Host("-oProxyCommand=x")


def test_a_batch_that_cannot_be_stored_raises_with_the_counts(conn, monkeypatch, tmp_path):
    _add(conn, "a", "One", "text")
    monkeypatch.setattr(embed_remote, "_Host", _FakeHost)
    monkeypatch.setattr(
        embed_remote, "_run_batch", lambda remote, root, batch, threads: [[0.5, 0.5]] * len(batch)
    )
    monkeypatch.setattr(embed_remote, "model_folder", lambda: tmp_path)

    with pytest.raises(embed_remote.HostUnavailable, match="stopped after 0 of 1") as failed:
        embed_remote.rebuild_on(conn, "computer")
    assert failed.value.counts.embedded == 0


# The rebuild command: an error, never a rebuild here, and a row in the sync log (#309)


def _rebuild(test_db, monkeypatch, *args):
    from lestash.cli.embeddings import app
    from lestash.core.config import Config
    from typer.testing import CliRunner

    monkeypatch.setattr(Config, "load", classmethod(lambda cls: test_db))
    return CliRunner().invoke(app, ["rebuild", *args])


def _log(test_db):
    with get_connection(test_db) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT status, items_added, error_message FROM sync_log "
                "WHERE source_type = ? ORDER BY id",
                (emb.LOG_SOURCE,),
            )
        ]


def test_a_host_that_is_down_stops_the_rebuild_with_an_error(test_db, monkeypatch, conn):
    _add(conn, "a", "One", "text")

    def down(conn, host, **kwargs):
        raise embed_remote.HostUnavailable(f"{host} unavailable: No route to host")

    def never(*args, **kwargs):
        raise AssertionError("the rebuild went on here")

    monkeypatch.setattr(embed_remote, "rebuild_on", down)
    monkeypatch.setattr(emb, "refresh_embeddings", never)

    result = _rebuild(test_db, monkeypatch, "--host", "computer")

    assert result.exit_code == 1
    assert "Rebuild stopped: computer unavailable: No route to host" in result.output
    assert _log(test_db) == [("failed", 0, "computer unavailable: No route to host")]


def test_a_host_that_stops_midway_logs_what_was_kept(test_db, monkeypatch, conn):
    def stops(conn, host, **kwargs):
        kept = emb.RefreshCounts(embedded=4000, pending=100)
        raise embed_remote.HostUnavailable(f"{host} stopped after 4000 of 4100: Killed", kept)

    monkeypatch.setattr(embed_remote, "rebuild_on", stops)

    result = _rebuild(test_db, monkeypatch, "--host", "computer")

    assert result.exit_code == 1
    assert "4000 item(s) embedded first are kept" in result.output
    assert _log(test_db) == [("failed", 4000, "computer stopped after 4000 of 4100: Killed")]


def test_a_rebuild_that_finishes_is_logged_as_completed(test_db, monkeypatch, conn):
    monkeypatch.setattr(
        embed_remote, "rebuild_on", lambda conn, host, **kw: emb.RefreshCounts(embedded=7)
    )

    result = _rebuild(test_db, monkeypatch, "--host", "computer")

    assert result.exit_code == 0, result.output
    assert _log(test_db) == [("completed", 7, None)]


def test_host_local_rebuilds_here_and_is_logged(test_db, monkeypatch, conn):
    _add(conn, "a", "One", "text")

    result = _rebuild(test_db, monkeypatch, "--host", "local")

    assert result.exit_code == 0, result.output
    assert _log(test_db) == [("completed", 1, None)]

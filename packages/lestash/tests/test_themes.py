"""Suggested themes (#266): communities of related items across sources, named from
their titles, kept as a collection or dismissed for good."""

import itertools
import json

import pytest
from lestash.core import themes
from lestash.core.database import get_connection


@pytest.fixture
def conn(test_db):
    with get_connection(test_db) as conn:
        yield conn


def _items(conn, titles, sources):
    ids = []
    for n, (title, source) in enumerate(zip(titles, sources, strict=True)):
        ids.append(
            conn.execute(
                "INSERT INTO items (source_type, source_id, title, content) VALUES (?, ?, ?, ?)",
                (source, f"{source}-{title}-{n}", title, "text"),
            ).lastrowid
        )
    return ids


def _relate(conn, ids, status="suggested", score=0.8):
    for a, b in itertools.combinations(ids, 2):
        conn.execute(
            "INSERT OR IGNORE INTO item_links (from_id, to_id, kind, evidence, origin, status) "
            "VALUES (?, ?, 'related', ?, 'linker:vectors', ?)",
            (min(a, b), max(a, b), json.dumps({"score": score, "found_by": [a]}), status),
        )
    conn.commit()


AGENTS = ["Intelligent agents", "Agents in practice", "Intelligent agent theory", "BDI agents"]
SOURCES = ["kobo", "arxiv", "google-drive", "claude"]
RL = ["Reinforcement learning", "Deep reinforcement", "Reward design", "Reinforcement notes"]


def _two_themes(conn):
    agents = _items(conn, AGENTS, SOURCES)
    rl = _items(conn, RL, SOURCES)
    _relate(conn, agents)
    _relate(conn, rl)
    return agents, rl


def _suggested(conn):
    return {tuple(json.loads(r["item_ids"])): r["name"] for r in themes.suggested(conn)}


def test_each_community_across_sources_is_a_theme(conn):
    agents, rl = _two_themes(conn)

    counts = themes.find_themes(conn)

    assert counts.suggested == 2
    found = _suggested(conn)
    assert set(found) == {tuple(agents), tuple(rl)}
    assert found[tuple(agents)].startswith("Agents") or "agents" in found[tuple(agents)].lower()
    assert "reinforcement" in found[tuple(rl)].lower()


def test_small_or_one_source_groups_are_left_out(conn):
    one_source = _items(conn, AGENTS, ["kobo"] * 4)
    small = _items(conn, RL[:3], SOURCES[:3])
    _relate(conn, one_source)
    _relate(conn, small)

    assert themes.find_themes(conn).suggested == 0


def test_rejected_links_dont_make_themes(conn):
    ids = _items(conn, AGENTS, SOURCES)
    _relate(conn, ids, status="rejected")

    assert themes.find_themes(conn).suggested == 0


def test_keeping_makes_a_collection_and_isnt_suggested_again(conn):
    agents, rl = _two_themes(conn)
    themes.find_themes(conn)
    theme_id = next(r["id"] for r in themes.suggested(conn) if json.loads(r["item_ids"]) == agents)

    collection_id = themes.keep(conn, theme_id, "Agents")
    conn.commit()

    (name,) = conn.execute("SELECT name FROM collections WHERE id = ?", (collection_id,)).fetchone()
    held = [
        r[0]
        for r in conn.execute(
            "SELECT item_id FROM collection_items WHERE collection_id = ? ORDER BY position",
            (collection_id,),
        )
    ]
    assert name == "Agents" and held == agents

    counts = themes.find_themes(conn)
    assert counts.suggested == 1 and counts.skipped == 1
    assert set(_suggested(conn)) == {tuple(rl)}


def test_dismissed_themes_and_ones_like_them_stay_away(conn):
    agents, _ = _two_themes(conn)
    themes.find_themes(conn)
    theme_id = next(r["id"] for r in themes.suggested(conn) if json.loads(r["item_ids"]) == agents)
    themes.dismiss(conn, theme_id)
    conn.commit()

    # One more item joins the group: still mostly the dismissed theme
    extra = _items(conn, ["Agents again"], ["youtube"])
    _relate(conn, [*agents, *extra])

    counts = themes.find_themes(conn)
    assert counts.skipped == 1
    assert all(tuple(agents) != ids[: len(agents)] for ids in _suggested(conn))


def test_only_suggested_themes_can_be_kept_or_dismissed(conn):
    agents, _ = _two_themes(conn)
    themes.find_themes(conn)
    theme_id = themes.suggested(conn)[0]["id"]
    themes.dismiss(conn, theme_id)

    with pytest.raises(themes.ThemeGone):
        themes.keep(conn, theme_id)
    with pytest.raises(themes.ThemeGone):
        themes.dismiss(conn, 99999)


def test_each_run_replaces_the_last_suggestions(conn):
    _two_themes(conn)
    themes.find_themes(conn)
    themes.find_themes(conn)

    assert len(themes.suggested(conn)) == 2


def test_names_come_from_shared_words_as_written():
    names = themes.name_themes(
        [
            ["LLMs and reasoning", "Reasoning with LLMs", "LLMs as judges b901"],
            ["Federated learning", "Federated attacks", "https://example.com/the-long-slug"],
            [""],
        ]
    )

    assert {w.lower() for w in names[0].split(", ")[:2]} == {"llms", "reasoning"}
    assert "LLMs" in names[0]
    assert "b901" not in names[0]
    assert names[1].startswith("Federated") and "slug" not in names[1]
    assert names[2] == "Untitled theme"

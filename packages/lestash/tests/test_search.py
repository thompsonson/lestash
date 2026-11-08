"""Search box input to FTS5 MATCH (lestash.core.search.fts_match_query)."""

from __future__ import annotations

import pytest
from lestash.cli import items as items_cli
from lestash.core.database import get_connection
from lestash.core.search import fts_match_query
from typer.testing import CliRunner

TITLES = {
    1: "The Dual-State Architecture for Reliable LLM Agents",
    2: "Release v2.0 notes",
    3: "C++ templates explained",
    4: "Don't panic: a guide",
    5: "Gödel, Escher, Bach",
    6: "Learning to learn",
    7: "Notes on title:case and a/b testing (draft)",
}


@pytest.fixture
def conn(test_db):
    """The real items/items_fts schema, holding one item per title."""
    with get_connection(test_db) as c:
        for n, title in TITLES.items():
            c.execute(
                "INSERT INTO items (id, source_type, source_id, title, content) "
                "VALUES (?, 'note', ?, ?, '')",
                (n, f"n{n}", title),
            )
        c.commit()
        yield c


def _hits(conn, q: str) -> set[int]:
    match = fts_match_query(q)
    if not match:
        return set()
    rows = conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (match,))
    return {r[0] for r in rows}


# Each query must be valid FTS5 (no OperationalError) and find the items listed
@pytest.mark.parametrize(
    ("q", "found"),
    [
        ("Dual-State Architecture", {1}),  # hyphen
        ("dual-state", {1}),
        ("v2.0", {2}),  # dot
        ("C++", {3, 7}),  # plus: the index drops "+", so "C"* also finds "case"
        ("don't", {4}),  # apostrophe
        ("title:case", {7}),  # colon: text, not a column filter
        ("a/b", {7}),  # slash
        ("(draft)", {7}),  # parentheses
        ('"Dual-State', {1}),  # unbalanced quote
        ('Dual"State', {1}),
        ("Gödel", {5}),  # unicode
        ("godel", {5}),  # diacritics folded, as the index does
        ("Escher,", {5}),
        ("learn", {6}),  # implicit prefix
        ("learn*", {6}),  # explicit prefix
        ('"reliable LLM"', {1}),  # phrase
        ('"LLM reliable"', set()),  # phrase order matters
        ('"dual state"*', {1}),
        ("panic OR Escher", {4, 5}),
        ("notes AND draft", {7}),
        ("notes NOT draft", {2}),
        ("AND", set()),  # bare operators search nothing
        ("NOT panic", set()),  # "everything but panic" can't be searched: nothing, not panic
        ("panic OR NOT guide", {4}),  # the excluded term is never searched for
        ("notes AND NOT draft", {2}),  # AND NOT is NOT
        ('don"t panic', {4}),  # a quote inside a word is an apostrophe
        ('don"t "a guide"', {4}),  # ...and a later phrase survives
        ("Go\u0308del", {5}),  # decomposed accent (NFD)
        ("panic AND OR guide", {4}),  # repeated operators collapse
        ("panic OR", {4}),
        ("++", set()),  # nothing searchable
        ('"', set()),
        ("NEAR", set()),  # FTS5 keyword, searched as a word
        ("and or not", set()),  # lower case: words, not operators
    ],
)
def test_query_is_valid_fts5_and_matches(conn, q, found) -> None:
    assert _hits(conn, q) == found


@pytest.mark.parametrize(
    ("q", "match"),
    [
        ("Dual-State Architecture", '"Dual State"* "Architecture"*'),
        ("v2.0", '"v2 0"*'),
        ("C++", '"C"*'),
        ("Gödel", '"Gödel"*'),
        ('"exact phrase" word', '"exact phrase" "word"*'),
        ('"a phrase"*', '"a phrase"*'),
        ("x OR y", '"x"* OR "y"*'),
        ("   ", ""),
        ("--- ...", ""),
        ("OR AND NOT", ""),
    ],
)
def test_match_expression(q, match) -> None:
    assert fts_match_query(q) == match


def test_cli_search_accepts_punctuation(test_db, conn, monkeypatch) -> None:
    monkeypatch.setattr(items_cli.Config, "load", classmethod(lambda cls: test_db))
    result = CliRunner().invoke(items_cli.app, ["search", "Dual-State"])
    assert result.exit_code == 0, result.output
    assert "Dual" in result.output  # the item's title, in the results table


def test_cli_search_with_nothing_searchable(test_db, conn, monkeypatch) -> None:
    monkeypatch.setattr(items_cli.Config, "load", classmethod(lambda cls: test_db))
    result = CliRunner().invoke(items_cli.app, ["search", "++"])
    assert result.exit_code == 0, result.output
    assert "No items found" in result.output


def test_cli_search_prints_markup_like_queries(test_db, conn, monkeypatch) -> None:
    """A query such as "[/x]" is text, not Rich markup."""
    monkeypatch.setattr(items_cli.Config, "load", classmethod(lambda cls: test_db))
    result = CliRunner().invoke(items_cli.app, ["search", "[/x]"])
    assert result.exit_code == 0, result.output
    assert "[/x]" in result.output

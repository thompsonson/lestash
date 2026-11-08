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
    8: "ре\u0301бенок играет",
    9: "हिन्दी भाषा",
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
        ("ре\u0301бенок", {8}),  # a combining accent NFC can't merge stays in the word
        ("ребенок", {8}),
        ("हिन्दी", {9}),  # marks the index splits on are split on here too
        ("\u201creliable LLM\u201d", {1}),  # typographic quotes mark a phrase
        ("\u201cLLM reliable\u201d", set()),
        ('"a"b "reliable LLM"', set()),  # a stray quote doesn't steal the phrase's quote
        ('"foo "reliable LLM"', set()),
        ('panic "', {4}),
        ("guide NOT - panic", {4}),  # NOT applied to "-", which can't be searched
        ("panic OR - Escher", {4, 5}),  # ...but an OR stays an OR
        ('("reliable LLM")', {1}),  # a phrase inside punctuation is still a phrase
        ("notes -draft", {2}),  # "-" excludes, as in web search
        ('notes -"testing (draft)"', {2}),
        ('notes NOT ("a/b testing")', {2}),  # "(" stuck to a phrase stays out of the way
        ("Dual-State", {1}),  # a "-" inside a word is not an exclusion
        ("notes NOT NOT draft", {2}),
        ("x\u0336y", set()),
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
        ("Dual-State Architecture", '"Dual-State"* "Architecture"*'),
        ("v2.0", '"v2.0"*'),
        ("C++", '"C++"*'),
        ("rust NOT - async", '"rust"* "async"*'),  # NOT went with the dropped "-"
        ("a NOT NOT b", '"a"* NOT "b"*'),
        ("a OR AND NOT b", '"a"*'),
        ("a OR - b", '"a"* OR "b"*'),
        ('"foo bar"baz', '"foo bar" "baz"*'),  # a phrase closed against a word
        ('"don"t panic"', '"don\'t panic"'),  # ...but an apostrophe inside a phrase stays
        ('"rock"n"roll"', "\"rock'n'roll\""),
        ('“don"t”', '"don\'t"'),
        ('"AND"', '"AND"'),  # a quoted operator is a word
        ('don"t', '"don\'t"*'),
        ('("x")', '"x"'),
        ('x -"a b"', '"x"* NOT "a b"'),
        ("x -y", '"x"* NOT "y"*'),
        ('rust NOT ("async io")', '"rust"* NOT "async io"'),
        ('a OR NOT ("b c")', '"a"*'),  # the excluded phrase is never searched
        ('-"a b"', ""),  # nothing to exclude from
        ('foo-"bar baz"', '"foo-"* "bar baz"'),
        ("Gödel", '"Gödel"*'),
        ('"exact phrase" word', '"exact phrase" "word"*'),
        ('"a phrase"*', '"a phrase"*'),
        ("x OR y", '"x"* OR "y"*'),
        ("   ", ""),
        ("--- ...", ""),
        ("OR AND NOT", ""),
        ('"foo bar "baz qux"', '"foo"* "bar"* "baz qux"'),
        ('say "hello" world"', '"say"* "hello" "world"*'),
        ("\u201creliable LLM\u201d", '"reliable LLM"'),
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


def test_long_queries_stay_inside_fts5_limits(conn) -> None:
    """FTS5 rejects expressions deeper than 256; the builder caps the terms."""
    q = "panic " + " NOT x" * 2000
    assert fts_match_query(q).count('"') // 2 == 64
    conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (fts_match_query(q),))


def test_cli_search_reports_index_failures(test_db, conn, monkeypatch) -> None:
    monkeypatch.setattr(items_cli.Config, "load", classmethod(lambda cls: test_db))
    conn.execute("DROP TABLE items_fts")
    conn.commit()
    result = CliRunner().invoke(items_cli.app, ["search", "panic"])
    assert result.exit_code == 1
    assert "Search failed" in result.output

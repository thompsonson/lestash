"""Keyword search: turn what a person types into a safe FTS5 MATCH expression, and run it.

Search boxes promise a small syntax: "quoted phrases", word* prefixes, and the operators
AND, OR and NOT. Anything else is plain text, so punctuation must never reach FTS5 as
syntax: "Dual-State", "v2.0", "C++", "don't" and "title:x" are all FTS5 syntax errors
when passed through as they are. Column filters (title:x), NEAR and ^ are not offered.

Each plain word becomes a quoted FTS5 string, which FTS5 splits with the index's own
tokenizer: "Dual-State" -> "Dual-State"*, the phrase dual state, as the indexed text was
split. Every word gets an implicit prefix, as the search box has always
done, so "learn" also finds "learning".
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Collection
from typing import NamedTuple

OPERATORS = frozenset({"AND", "OR", "NOT"})
# FTS5 rejects expressions deeper than 256; far more terms than anyone types
MAX_TERMS = 64

# Typographic quotes, as phone keyboards send them, mark phrases like '"'
_QUOTES = str.maketrans({c: '"' for c in "\u201c\u201d\u201e\u201f\u00ab\u00bb\uff02"})


class _Token(NamedTuple):
    text: str
    phrase: bool  # from "quotes": searched exactly, not as a prefix unless starred
    star: bool  # a phrase followed by *


def _searchable(text: str) -> bool:
    """Whether text holds a letter or digit, so the index's tokenizer finds a token in it.
    FTS5 splits the text itself; this only drops words that would be empty phrases."""
    return any(c.isalnum() for c in text)


def _closing_quote(q: str, start: int) -> int | None:
    """Where the phrase opened by the quote at start ends, or None if that quote is stray.

    The phrase ends at the first quote followed by the end or a non-word character. A
    quote between two letters or digits inside it is an apostrophe ("don"t panic").

    If no such closing quote comes first, the first inner quote closes a phrase of more
    than one word ("foo bar"baz, "machine learning"s "deep nets"). For a single word the
    quote at start was stray and the inner one an apostrophe ("don"t). A quote that can
    only open (after a space, before a letter) also ends the search: "foo bar "baz qux"
    is two words and a phrase.
    """
    first_inner: int | None = None
    for j in range(start + 1, len(q)):
        if q[j] != '"':
            continue
        word_before = q[j - 1].isalnum()
        word_after = j + 1 < len(q) and q[j + 1].isalnum()
        if not word_after:
            return j
        if not word_before:
            break
        if first_inner is None:
            first_inner = j
    if first_inner is not None and any(c.isspace() for c in q[start + 1 : first_inner]):
        return first_inner
    return None


def _tokens(q: str) -> list[_Token]:
    """q split, left to right, into words (runs of non-space characters) and phrases.

    A quote opens a phrase at the start or after a non-word character, when a closing
    quote follows (see _closing_quote). Any other quote is an apostrophe between two
    letters or digits (don"t), and a separator elsewhere.

    A "-" in front of a phrase excludes it, as in web search: -"draft copy" becomes NOT
    and the phrase. A "-" in front of a word stays part of the word, since searches for
    flags and numbers (--dry-run, rm -rf, -40) are common; NOT excludes a word. Other
    punctuation stuck to the front of a phrase, like the "(" in ("a b"), is dropped, so
    it can't come between an operator and the phrase it applies to.
    """
    tokens: list[_Token] = []
    word: list[str] = []

    def end_word() -> None:
        if word:
            tokens.append(_Token("".join(word), phrase=False, star=False))
            word.clear()

    i, n = 0, len(q)
    while i < n:
        c = q[i]
        if c.isspace():
            end_word()
        elif c != '"':
            word.append(c)
        else:
            word_before = i > 0 and q[i - 1].isalnum()
            end = None if word_before else _closing_quote(q, i)
            if end is not None:
                lead = "".join(word)
                if _searchable(lead):
                    end_word()  # foo-"bar baz": the word foo, then the phrase
                else:
                    word.clear()  # "(" or "-" stuck to the phrase is not a word
                    if "-" in lead:  # -"a b", -("a b")
                        tokens.append(_Token("NOT", phrase=False, star=False))
                star = end + 1 < n and q[end + 1] == "*"
                text = q[i + 1 : end].replace('"', "'")  # inner quotes are apostrophes
                tokens.append(_Token(text, phrase=True, star=star))
                i = end + 1 + star
                continue
            if word_before and i + 1 < n and q[i + 1].isalnum():
                word.append("'")
            else:
                end_word()
        i += 1
    end_word()
    return tokens


def _phrase(text: str, prefix: bool) -> str:
    """text as one FTS5 string, which FTS5 tokenizes as it tokenized the indexed text:
    "Dual-State" is the phrase dual state. _tokens leaves no quotes in text; any that did
    reach here would be doubled, as FTS5 escapes them, rather than end the string."""
    return '"' + text.replace('"', '""') + '"' + ("*" if prefix else "")


def fts_match_query(q: str) -> str:
    """The FTS5 MATCH expression for search box input q; "" when nothing is searchable.

    - Input is NFC-normalised, and typographic quotes count as '"'.
    - "Quoted phrases" stay phrases, and a * right after one is kept. A quote between two
      letters is an apostrophe (don"t, "don"t panic"); stray quotes are dropped. See
      _tokens and _closing_quote.
    - AND, OR and NOT (upper case, as FTS5 reads them) stay operators between two terms.
      Leading, trailing and repeated operators are dropped; "AND NOT" is read as NOT.
      A NOT with no term before it (or after OR) is dropped with the term it would
      exclude, since FTS5 can't search "everything but x": "NOT x" searches nothing,
      and "a OR NOT b" searches a, never b itself.
    - Every other word is quoted, so FTS5 tokenizes it exactly as it tokenized the indexed
      text, and given a prefix *. A word or phrase with no letter or digit ("++", "-") is
      dropped, with an AND or NOT just before it; an OR stays ("a OR - b" is a OR b).
    - Only the first MAX_TERMS terms are kept, so FTS5's depth limit is never reached.
    """
    terms: list[str] = []
    for token in _tokens(unicodedata.normalize("NFC", q).translate(_QUOTES)):
        if not token.phrase and token.text in OPERATORS:
            terms.append(token.text)
        elif _searchable(token.text):
            terms.append(_phrase(token.text, prefix=token.star or not token.phrase))
        elif terms and terms[-1] in ("AND", "NOT"):
            # "rust NOT - async": the NOT was about "-", not "async"
            terms.pop()

    # Operators only between two search terms: drop leading, trailing and repeated ones.
    # A NOT with no term before it also drops the term it would have excluded.
    out: list[str] = []
    kept = 0
    skip_next_term = False
    for term in terms:
        if term in OPERATORS:
            if term == "NOT" and out and out[-1] == "AND":
                out[-1] = "NOT"  # "a AND NOT b" is FTS5's "a NOT b"
                continue
            if not out or out[-1] == "OR":
                # Nothing for NOT to exclude from: drop it with the term it excludes
                skip_next_term = skip_next_term or term == "NOT"
                continue
            if out[-1] in OPERATORS:
                continue  # "a NOT NOT b" is "a NOT b"
            out.append(term)
        elif skip_next_term:
            skip_next_term = False
        elif kept < MAX_TERMS:
            out.append(term)
            kept += 1
    while out and out[-1] in OPERATORS:
        out.pop()
    return " ".join(out)


def keyword_search(
    conn: sqlite3.Connection,
    q: str,
    limit: int,
    *,
    top_level_only: bool = False,
    snippets: bool = False,
    kinds: Collection[str] | None = None,
    by_owner: bool = False,
) -> list[sqlite3.Row]:
    """Items matching search box input q, best first; [] when nothing is searchable.

    With snippets, each row also has search_snippet: the matching part of its content,
    with matches between << and >>. Raises sqlite3.Error if the index itself fails.
    With kinds, only items of those kinds; by_owner reads a part's kind as its
    parent's, as grouped search shows a part under the item it belongs to.
    """
    match = fts_match_query(q)
    if not match:
        return []
    snippet = ", snippet(items_fts, 1, '<<', '>>', '...', 32) AS search_snippet"
    sql = (
        f"SELECT items.*{snippet if snippets else ''} FROM items"
        " JOIN items_fts ON items.id = items_fts.rowid"
        " WHERE items_fts MATCH ? AND items.held = 1"
        f"{' AND items.parent_id IS NULL' if top_level_only else ''}"
        f"{_kind_filter(kinds, by_owner)}"
        " ORDER BY rank LIMIT ?"
    )
    return conn.execute(sql, (match, *(sorted(kinds) if kinds else ()), limit)).fetchall()


def _kind_filter(kinds: Collection[str] | None, by_owner: bool) -> str:
    if not kinds:
        return ""
    marks = ",".join("?" * len(kinds))
    kind = (
        "COALESCE((SELECT p.kind FROM items p WHERE p.id = items.parent_id), items.kind)"
        if by_owner
        else "items.kind"
    )
    return f" AND {kind} IN ({marks})"

"""Keyword search: turn what a person types into a safe FTS5 MATCH expression, and run it.

Search boxes promise a small syntax: "quoted phrases", word* prefixes, and the operators
AND, OR and NOT. Anything else is plain text, so punctuation must never reach FTS5 as
syntax: "Dual-State", "v2.0", "C++", "don't" and "title:x" are all FTS5 syntax errors
when passed through as they are. Column filters (title:x), NEAR and ^ are not offered.

Each plain word is split the way the index's tokenizer (unicode61) splits text, and
becomes a quoted phrase: "Dual-State" -> "Dual State"*, which matches the indexed tokens
dual, state in order. Every word gets an implicit prefix, as the search box has always
done, so "learn" also finds "learning".
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

OPERATORS = frozenset({"AND", "OR", "NOT"})
# FTS5 rejects expressions deeper than 256; far more terms than anyone types
MAX_TERMS = 64

# Typographic quotes, as phone keyboards send them, mark phrases like '"'
_QUOTES = str.maketrans({c: '"' for c in "“”„‟«»＂"})
# A quoted phrase (optionally followed by *), or a run of non-space characters
_TOKEN = re.compile(r'"([^"]*)"(\*?)|(\S+)')


def _is_token_char(c: str) -> bool:
    """What unicode61 keeps in a token: letters, numbers, private use, and the combining
    accents it folds away (U+0300-U+036F). Everything else, "_" included, separates."""
    cat = unicodedata.category(c)
    return cat[0] in "LN" or cat == "Co" or (cat == "Mn" and "̀" <= c <= "ͯ")


def _word_parts(text: str) -> list[str]:
    """text split into runs of token characters, as the index splits it."""
    parts: list[str] = []
    run: list[str] = []
    for c in text:
        if _is_token_char(c):
            run.append(c)
        elif run:
            parts.append("".join(run))
            run = []
    if run:
        parts.append("".join(run))
    return parts


def _drop_stray_quotes(q: str) -> str:
    """Make quotes pair up: a quote between two token characters (don"t) is an apostrophe,
    and a lone quote left over is dropped. A quote opens a phrase at the start or after a
    space, and closes one at the end or before a non-word character. A quote that can do
    neither is dropped, and an open quote is dropped when a later quote can only open, so
    a stray quote never steals a later phrase's quote."""
    chars = list(q)
    for i, c in enumerate(chars):
        if (
            c == '"'
            and 0 < i < len(chars) - 1
            and _is_token_char(chars[i - 1])
            and _is_token_char(chars[i + 1])
        ):
            chars[i] = "'"
    open_at: int | None = None
    for i, c in enumerate(chars):
        if c != '"':
            continue
        can_open = i == 0 or chars[i - 1].isspace()
        can_close = i == len(chars) - 1 or not _is_token_char(chars[i + 1])
        if open_at is None and can_open:
            open_at = i
        elif open_at is not None and can_close:
            open_at = None
        elif open_at is not None and can_open:
            # Only an opening quote fits here, so the earlier one was stray
            chars[open_at] = " "
            open_at = i
        else:
            chars[i] = " "
    if open_at is not None:
        chars[open_at] = " "
    return "".join(chars)


def _phrase(parts: list[str], prefix: bool) -> str:
    """Parts as one FTS5 string; they hold only token characters, so no escaping."""
    return '"' + " ".join(parts) + '"' + ("*" if prefix else "")


def fts_match_query(q: str) -> str:
    """The FTS5 MATCH expression for search box input q; "" when nothing is searchable.

    - Input is NFC-normalised, and typographic quotes count as '"'.
    - A balanced "quoted phrase" stays a phrase; a trailing * after it is kept. A quote
      inside a word (don"t) is an apostrophe; a stray quote is dropped.
    - AND, OR and NOT (upper case, as FTS5 reads them) stay operators between two terms.
      Leading, trailing and repeated operators are dropped; "AND NOT" is read as NOT.
      A NOT with no term before it (or after OR) is dropped with the term it would
      exclude, since FTS5 can't search "everything but x": "NOT x" searches nothing,
      and "a OR NOT b" searches a, never b itself.
    - Every other word is split as the index splits text, quoted, and given a prefix *.
      A word with nothing searchable in it ("++", "-") is dropped.
    - Only the first MAX_TERMS terms are kept, so FTS5's depth limit is never reached.
    """
    q = _drop_stray_quotes(unicodedata.normalize("NFC", q).translate(_QUOTES))

    terms: list[str] = []
    for m in _TOKEN.finditer(q):
        quoted, star, word = m.group(1), m.group(2), m.group(3)
        if quoted is not None:
            parts = _word_parts(quoted)
            if parts:
                terms.append(_phrase(parts, prefix=bool(star)))
        elif word in OPERATORS:
            terms.append(word)
        else:
            parts = _word_parts(word)
            if parts:
                terms.append(_phrase(parts, prefix=True))

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
            if not out or out[-1] in OPERATORS:
                skip_next_term = skip_next_term or term == "NOT"
                continue
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
) -> list[sqlite3.Row]:
    """Items matching search box input q, best first; [] when nothing is searchable.

    With snippets, each row also has search_snippet: the matching part of its content,
    with matches between << and >>. Raises sqlite3.Error if the index itself fails.
    """
    match = fts_match_query(q)
    if not match:
        return []
    snippet = ", snippet(items_fts, 1, '<<', '>>', '...', 32) AS search_snippet"
    sql = (
        f"SELECT items.*{snippet if snippets else ''} FROM items"
        " JOIN items_fts ON items.id = items_fts.rowid"
        " WHERE items_fts MATCH ?"
        f"{' AND items.parent_id IS NULL' if top_level_only else ''}"
        " ORDER BY rank LIMIT ?"
    )
    return conn.execute(sql, (match, limit)).fetchall()

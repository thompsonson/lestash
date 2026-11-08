"""Turn what a person types into a safe FTS5 MATCH expression.

Search boxes promise a small syntax: "quoted phrases", word* prefixes, and the operators
AND, OR and NOT. Anything else is plain text, so punctuation must never reach FTS5 as
syntax: "Dual-State", "v2.0", "C++", "don't" and "title:x" are all FTS5 syntax errors
when passed through as they are.

Each plain word is split the way the index's tokenizer (unicode61) splits text, into runs
of letters and digits, and becomes a quoted phrase: "Dual-State" -> "Dual State"*, which
matches the indexed tokens dual, state in order. Every word gets an implicit prefix, as
the search box has always done, so "learn" also finds "learning".
"""

from __future__ import annotations

import re
import unicodedata

OPERATORS = frozenset({"AND", "OR", "NOT"})

# A quoted phrase (optionally followed by *), or a run of non-space characters
_TOKEN = re.compile(r'"([^"]*)"(\*?)|(\S+)')
# What unicode61 keeps as token characters: letters and digits, not "_" or punctuation
_WORD_PARTS = re.compile(r"[^\W_]+")
# A quote with a letter or digit on both sides is inside a word (don"t), not a phrase mark
_INNER_QUOTE = re.compile(r'(?<=[^\W_])"(?=[^\W_])')


def _phrase(parts: list[str], prefix: bool) -> str:
    """Parts as one FTS5 string; parts hold only letters and digits, so no escaping."""
    return '"' + " ".join(parts) + '"' + ("*" if prefix else "")


def fts_match_query(q: str) -> str:
    """The FTS5 MATCH expression for search box input q; "" when nothing is searchable.

    - A balanced "quoted phrase" stays a phrase; a trailing * after it is kept.
    - A quote inside a word (don"t) is punctuation. If the rest are unbalanced, the last
      one is punctuation too.
    - AND, OR and NOT (upper case, as FTS5 reads them) stay operators between two terms.
      Leading, trailing and repeated operators are dropped; "AND NOT" is read as NOT.
      A NOT with no term before it (or after OR) is dropped with the term it would
      exclude, since FTS5 can't search "everything but x": "NOT x" searches nothing,
      and "a OR NOT b" searches a, never b itself.
    - Every other word is split into letter and digit runs, quoted, and given a prefix *.
      A word with no letters or digits ("++", "-") is dropped.
    - Input is NFC-normalised first, so a decomposed accent (o + combining diaeresis, as
      some keyboards send) stays inside its word, as the index's tokenizer keeps it.
    """
    q = unicodedata.normalize("NFC", q)
    q = _INNER_QUOTE.sub("'", q)  # read as an apostrophe: don"t is searched like don't
    if q.count('"') % 2:
        i = q.rfind('"')
        q = q[:i] + " " + q[i + 1 :]

    terms: list[str] = []
    for m in _TOKEN.finditer(q):
        quoted, star, word = m.group(1), m.group(2), m.group(3)
        if quoted is not None:
            parts = _WORD_PARTS.findall(quoted)
            if parts:
                terms.append(_phrase(parts, prefix=bool(star)))
        elif word in OPERATORS:
            terms.append(word)
        else:
            parts = _WORD_PARTS.findall(word)
            if parts:
                terms.append(_phrase(parts, prefix=True))

    # Operators only between two search terms: drop leading, trailing and repeated ones.
    # A NOT with no term before it also drops the term it would have excluded.
    out: list[str] = []
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
        else:
            out.append(term)
    while out and out[-1] in OPERATORS:
        out.pop()
    return " ".join(out)

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

OPERATORS = frozenset({"AND", "OR", "NOT"})

# A quoted phrase (optionally followed by *), or a run of non-space characters
_TOKEN = re.compile(r'"([^"]*)"(\*?)|(\S+)')
# What unicode61 keeps as token characters: letters and digits, not "_" or punctuation
_WORD_PARTS = re.compile(r"[^\W_]+")


def _phrase(parts: list[str], prefix: bool) -> str:
    """Parts as one FTS5 string; parts hold only letters and digits, so no escaping."""
    return '"' + " ".join(parts) + '"' + ("*" if prefix else "")


def fts_match_query(q: str) -> str:
    """The FTS5 MATCH expression for search box input q; "" when nothing is searchable.

    - A balanced "quoted phrase" stays a phrase; a trailing * after it is kept.
    - A lone, unbalanced quote is treated as punctuation.
    - AND, OR and NOT (upper case, as FTS5 reads them) stay operators, except at the start
      or end, or next to another operator, where they would be a syntax error.
    - Every other word is split into letter and digit runs, quoted, and given a prefix *.
      A word with no letters or digits ("++", "-") is dropped.
    """
    if q.count('"') % 2:
        # Drop the last quote, so earlier balanced phrases survive
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

    # Operators only between two search terms: drop leading, trailing and repeated ones
    out: list[str] = []
    for term in terms:
        if term in OPERATORS and (not out or out[-1] in OPERATORS):
            continue
        out.append(term)
    while out and out[-1] in OPERATORS:
        out.pop()
    return " ".join(out)

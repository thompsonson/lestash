"""Extract hyperlinks from a PDF and rewrite plain anchor text into markdown links.

Links in PDFs live as annotation objects (`/Type /Annot /Subtype /Link` with
`/A /URI`) — invisible to any text extractor including Docling. We get them
from PyMuPDF's `page.get_links()`, then resolve their anchor text by clipping
text from the link rectangle.

Replacement walks the markdown left-to-right with a cursor, consuming each
link in document order. Comparison is two-pass:

1. **Strict normalisation** — collapse whitespace, drop soft hyphens, casefold.
   Catches the common case where Docling reflowed the anchor across newlines.
2. **Aggressive fallback** (only if strict misses) — NFKC unicode normalise
   then treat any non-alphanumeric character as whitespace. Catches Barnes &
   Noble, DOIs with internal punctuation, smart-quoted apostrophes, and other
   reformatting Docling does to surrounding markup. See #143.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pymupdf


@dataclass
class Link:
    page: int
    bbox: tuple[float, float, float, float]
    uri: str
    anchor_text: str  # raw text PyMuPDF reads from the link rectangle


_WS = re.compile(r"\s+")


def _normalise(text: str, *, aggressive: bool = False) -> str:
    """Canonicalise text for fuzzy comparison.

    Strict mode: collapse whitespace, drop soft hyphens, casefold.
    Aggressive mode: NFKC normalise (smart quotes/ligatures/&amp;), then
    treat all non-alphanumeric chars as whitespace before collapsing.
    """
    if aggressive:
        text = unicodedata.normalize("NFKC", text)
        text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    text = text.replace("­", "")  # soft hyphen
    text = _WS.sub(" ", text).strip()
    return text.casefold()


def _is_kept(ch: str, *, aggressive: bool) -> bool:
    """True if `ch` should appear in the normalised stream as itself (vs being
    treated as whitespace or dropped)."""
    if ch == "­" or ch.isspace():
        return False
    return not (aggressive and not ch.isalnum())


def extract_links(doc: pymupdf.Document) -> list[Link]:
    """Return all URI links in the document, in page+reading order."""
    links: list[Link] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        for raw in page.get_links():
            uri = raw.get("uri")
            if not uri:
                continue
            rect = raw.get("from")
            if rect is None:
                continue
            bbox = (rect.x0, rect.y0, rect.x1, rect.y1)
            anchor = page.get_text(clip=rect).strip()
            links.append(Link(page=page_num, bbox=bbox, uri=uri, anchor_text=anchor))
    return links


def apply_links(markdown: str, links: list[Link]) -> str:
    """Rewrite the first occurrence of each link's anchor text into a markdown
    link, walking left-to-right.

    Tries strict matching first, falls back to aggressive matching for that
    one link if strict fails. If both miss, the link is appended in a
    `<!-- unmatched-links -->` block at the end so nothing is lost.
    """
    cursor = 0
    out: list[str] = []
    unmatched: list[Link] = []

    for link in links:
        anchor = link.anchor_text
        if not anchor:
            unmatched.append(link)
            continue

        match_start = _find_normalised(markdown, anchor, cursor, aggressive=False)
        if match_start is None:
            match_start = _find_normalised(markdown, anchor, cursor, aggressive=True)
        if match_start is None:
            unmatched.append(link)
            continue

        # Use aggressive end-finder iff strict didn't find a start. This keeps
        # the fast/precise path on the strict-match cases and only relaxes for
        # the awkward ones.
        used_aggressive = _find_normalised(markdown, anchor, cursor, aggressive=False) is None
        match_end = _find_normalised_end(markdown, anchor, match_start, aggressive=used_aggressive)
        out.append(markdown[cursor:match_start])
        out.append(f"[{markdown[match_start:match_end]}]({link.uri})")
        cursor = match_end

    out.append(markdown[cursor:])
    rewritten = "".join(out)

    if unmatched:
        rewritten = (
            rewritten.rstrip()
            + "\n\n<!-- unmatched-links -->\n"
            + "\n".join(f"- [{link.anchor_text or link.uri}]({link.uri})" for link in unmatched)
            + "\n"
        )

    return rewritten


def _find_normalised(haystack: str, needle: str, start: int, *, aggressive: bool) -> int | None:
    """Locate `needle` in `haystack[start:]` using normalised comparison.

    Returns the byte offset in `haystack` where the match begins, or None.
    """
    norm_needle = _normalise(needle, aggressive=aggressive)
    if not norm_needle:
        return None

    if aggressive:
        haystack_for_chars = unicodedata.normalize("NFKC", haystack[start:])
        offset_to_orig = _build_nfkc_offset_map(haystack, start)
    else:
        haystack_for_chars = haystack[start:]
        offset_to_orig = list(range(start, len(haystack)))

    norm_chars: list[tuple[int, str]] = []
    prev_was_space = True
    for local_idx, ch in enumerate(haystack_for_chars):
        orig_idx = offset_to_orig[local_idx] if local_idx < len(offset_to_orig) else len(haystack)
        if not _is_kept(ch, aggressive=aggressive):
            if not prev_was_space:
                norm_chars.append((orig_idx, " "))
            prev_was_space = True
        else:
            norm_chars.append((orig_idx, ch.casefold()))
            prev_was_space = False

    while norm_chars and norm_chars[0][1] == " ":
        norm_chars.pop(0)
    while norm_chars and norm_chars[-1][1] == " ":
        norm_chars.pop()
    norm_str = "".join(c for _, c in norm_chars)

    idx = norm_str.find(norm_needle)
    if idx < 0:
        return None
    return norm_chars[idx][0]


def _find_normalised_end(haystack: str, needle: str, match_start: int, *, aggressive: bool) -> int:
    """Given a match start in `haystack`, return the end offset that covers
    enough characters to span the normalised `needle`."""
    norm_needle = _normalise(needle, aggressive=aggressive)
    consumed_norm = 0
    i = match_start
    prev_was_space = True
    while i < len(haystack) and consumed_norm < len(norm_needle):
        ch = haystack[i]
        if not _is_kept(ch, aggressive=aggressive):
            if not prev_was_space:
                consumed_norm += 1
            prev_was_space = True
        else:
            consumed_norm += 1
            prev_was_space = False
        i += 1
    return i


def _build_nfkc_offset_map(haystack: str, start: int) -> list[int]:
    """For each character index in `NFKC(haystack[start:])`, return the
    corresponding original byte offset in `haystack`. NFKC may expand single
    code points (e.g. ligatures `ﬁ` → `fi`); we map every expanded char back
    to the original source position so the markdown rewrite hits the right
    span."""
    out: list[int] = []
    for orig_local, ch in enumerate(haystack[start:]):
        nfkc = unicodedata.normalize("NFKC", ch)
        for _ in nfkc:
            out.append(start + orig_local)
    return out

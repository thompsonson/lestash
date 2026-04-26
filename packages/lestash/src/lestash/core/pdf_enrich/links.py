"""Extract hyperlinks from a PDF and rewrite plain anchor text into markdown links.

Links in PDFs live as annotation objects (`/Type /Annot /Subtype /Link` with
`/A /URI`) — invisible to any text extractor including Docling. We get them
from PyMuPDF's `page.get_links()`, then resolve their anchor text by clipping
text from the link rectangle.

Replacement walks the markdown left-to-right with a cursor, consuming each
link in document order. Comparison is done on a normalised form (whitespace
collapsed, soft hyphens removed, leading/trailing trimmed) because Docling
reflows lines that PyMuPDF reads as one continuous string.
"""

from __future__ import annotations

import re
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


def _normalise(text: str) -> str:
    """Canonicalise text for fuzzy comparison: collapse whitespace, strip soft
    hyphens, trim, lowercase."""
    text = text.replace("­", "")  # soft hyphen
    text = _WS.sub(" ", text).strip()
    return text.casefold()


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

    If the anchor text cannot be located in the markdown (Docling reflowed it
    away, or the anchor was empty), the link is appended at the very end of
    the document inside a `<!-- unmatched-links -->` block so nothing is lost.
    """
    cursor = 0
    out: list[str] = []
    unmatched: list[Link] = []

    for link in links:
        anchor = link.anchor_text
        if not anchor:
            unmatched.append(link)
            continue
        match_start = _find_normalised(markdown, anchor, cursor)
        if match_start is None:
            unmatched.append(link)
            continue
        match_end = _find_normalised_end(markdown, anchor, match_start)
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


def _find_normalised(haystack: str, needle: str, start: int) -> int | None:
    """Locate `needle` in `haystack[start:]` using normalised comparison.

    Returns the byte offset in `haystack` where the match begins, or None.
    Implementation: walk forward through `haystack[start:]` accumulating
    normalised characters and compare against the normalised needle.
    """
    norm_needle = _normalise(needle)
    if not norm_needle:
        return None

    # Build a parallel array: for each non-whitespace char in haystack[start:],
    # record (haystack_index, normalised_char).
    norm_chars: list[tuple[int, str]] = []
    prev_was_space = True
    for i in range(start, len(haystack)):
        ch = haystack[i]
        if ch == "­":
            continue
        if ch.isspace():
            if not prev_was_space:
                norm_chars.append((i, " "))
            prev_was_space = True
        else:
            norm_chars.append((i, ch.casefold()))
            prev_was_space = False

    norm_str = "".join(c for _, c in norm_chars).strip()
    # Re-derive offset map after strip
    while norm_chars and norm_chars[0][1] == " ":
        norm_chars.pop(0)
    while norm_chars and norm_chars[-1][1] == " ":
        norm_chars.pop()

    idx = norm_str.find(norm_needle)
    if idx < 0:
        return None
    return norm_chars[idx][0]


def _find_normalised_end(haystack: str, needle: str, match_start: int) -> int:
    """Given a match start in `haystack`, return the end offset that covers
    enough characters to span the normalised `needle`."""
    norm_needle = _normalise(needle)
    consumed_norm = 0
    i = match_start
    prev_was_space = True
    while i < len(haystack) and consumed_norm < len(norm_needle):
        ch = haystack[i]
        if ch == "­":
            i += 1
            continue
        if ch.isspace():
            if not prev_was_space:
                consumed_norm += 1
            prev_was_space = True
        else:
            consumed_norm += 1
            prev_was_space = False
        i += 1
    return i

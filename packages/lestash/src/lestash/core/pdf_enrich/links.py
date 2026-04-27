"""Extract hyperlinks from a PDF and rewrite plain anchor text into markdown links.

Links in PDFs live as annotation objects (`/Type /Annot /Subtype /Link` with
`/A /URI`) — invisible to any text extractor including Docling. We get them
from PyMuPDF's `page.get_links()`, then resolve their anchor text by clipping
text from the link rectangle.

Replacement walks the markdown left-to-right with a cursor, consuming each
link in document order. Comparison is two-pass:

1. **Strict normalisation** — collapse whitespace, drop soft hyphens, casefold.
   Catches the common case where Docling reflowed the anchor across newlines.
2. **Aggressive fallback** (only if strict misses) — HTML entity decode (so
   `&amp;` matches `&`), NFKC unicode normalise (so smart-quoted `O’Reilly`
   matches `O'Reilly`), then treat all non-alphanumeric chars as whitespace.
   See #143.

The matcher uses a span-aware stream — every normalised char carries the
original `(orig_start, orig_end)` half-open span in the markdown that
produced it, so the rewrite always wraps the correct source text even when
the normalisation step expanded or collapsed character counts (e.g. `&amp;`
collapsing 5 chars to 1, or NFKC expanding `ﬁ` to two).
"""

from __future__ import annotations

import html
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
_ENTITY_RE = re.compile(r"&(?:#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


@dataclass
class _StreamChar:
    """A single normalised character carrying back its source span."""

    orig_start: int
    orig_end: int
    char: str


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

        span = _locate(markdown, anchor, cursor, aggressive=False)
        if span is None:
            span = _locate(markdown, anchor, cursor, aggressive=True)
        if span is None:
            unmatched.append(link)
            continue

        match_start, match_end = span
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


# --- Internal: matching --------------------------------------------------


def _normalise(text: str, *, aggressive: bool) -> str:
    """Canonicalise text for comparison.

    Strict: collapse whitespace, drop soft hyphens, casefold.
    Aggressive: also HTML-entity-decode + NFKC normalise + treat all
    non-alphanumeric chars as whitespace.
    """
    if aggressive:
        text = html.unescape(text)
        text = unicodedata.normalize("NFKC", text)
        text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    text = text.replace("­", "")  # soft hyphen
    text = _WS.sub(" ", text).strip()
    return text.casefold()


def _is_kept(ch: str, *, aggressive: bool) -> bool:
    """True if `ch` should appear in the normalised stream as itself."""
    if ch == "­" or ch.isspace():
        return False
    return not (aggressive and not ch.isalnum())


def _locate(haystack: str, needle: str, cursor: int, *, aggressive: bool) -> tuple[int, int] | None:
    """Find the first occurrence of `needle` in `haystack[cursor:]` under the
    chosen normalisation. Returns the (start, end) half-open span in the
    *original* haystack covering the matched source text, or None."""
    norm_needle = _normalise(needle, aggressive=aggressive)
    if not norm_needle:
        return None
    stream = _normalise_stream(
        _build_stream(haystack, cursor, aggressive=aggressive), aggressive=aggressive
    )
    norm_str = "".join(s.char for s in stream)
    idx = norm_str.find(norm_needle)
    if idx < 0:
        return None
    start_orig = stream[idx].orig_start
    end_orig = stream[idx + len(norm_needle) - 1].orig_end
    return (start_orig, end_orig)


def _build_stream(haystack: str, start: int, *, aggressive: bool) -> list[_StreamChar]:
    """Produce a stream of (orig_start, orig_end, char) triples from
    `haystack[start:]`. In aggressive mode, HTML entities are decoded into
    their target characters and NFKC normalisation is applied — the
    `(orig_start, orig_end)` span always covers the source text in the
    original haystack so a downstream rewrite hits the right region.
    """
    out: list[_StreamChar] = []
    i = start
    end = len(haystack)
    while i < end:
        if aggressive:
            m = _ENTITY_RE.match(haystack, i)
            if m:
                decoded = html.unescape(m.group())
                if decoded != m.group():
                    nfkc = unicodedata.normalize("NFKC", decoded)
                    span_end = i + len(m.group())
                    for ch in nfkc:
                        out.append(_StreamChar(i, span_end, ch))
                    i = span_end
                    continue
            ch = haystack[i]
            nfkc = unicodedata.normalize("NFKC", ch)
            for n_ch in nfkc:
                out.append(_StreamChar(i, i + 1, n_ch))
        else:
            out.append(_StreamChar(i, i + 1, haystack[i]))
        i += 1
    return out


def _normalise_stream(stream: list[_StreamChar], *, aggressive: bool) -> list[_StreamChar]:
    """Apply whitespace-collapse / soft-hyphen-drop / casefold (and in
    aggressive mode treat non-alphanumeric as whitespace), preserving the
    source spans on each kept char."""
    out: list[_StreamChar] = []
    prev_was_space = True
    for sc in stream:
        if not _is_kept(sc.char, aggressive=aggressive):
            if not prev_was_space:
                out.append(_StreamChar(sc.orig_start, sc.orig_end, " "))
            prev_was_space = True
        else:
            out.append(_StreamChar(sc.orig_start, sc.orig_end, sc.char.casefold()))
            prev_was_space = False
    while out and out[0].char == " ":
        out.pop(0)
    while out and out[-1].char == " ":
        out.pop()
    return out

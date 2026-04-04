"""Parse saved HTML pages into Le Stash items.

Supports auto-detection of page type (Gemini, etc.) and extraction
of structured content from the rendered DOM using BeautifulSoup.
"""

import hashlib
import logging
import re

from bs4 import BeautifulSoup
from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

# Page type constants
TYPE_GEMINI = "gemini"
TYPE_CHATGPT = "chatgpt"
TYPE_ARTICLE = "article"
TYPE_UNKNOWN = "unknown"


def _clean_html(html: str) -> str:
    """Strip script, style, and SVG elements to reduce noise before parsing."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()
    return str(soup)


def detect_html_type(html: str) -> str:
    """Detect the type of HTML page from its content.

    Uses lightweight string checks before full parsing.
    """
    # Gemini: custom elements used in the Angular app
    if "user-query" in html and "model-response" in html:
        return TYPE_GEMINI

    # ChatGPT: stub for future implementation
    if "data-message-author-role" in html:
        return TYPE_CHATGPT

    # Article: presence of <article> tag or og:type article
    if "<article" in html or 'og:type" content="article' in html:
        return TYPE_ARTICLE

    return TYPE_UNKNOWN


def parse_gemini_html(
    html: str,
    source_url: str | None = None,
    notes: str | None = None,
) -> list[ItemCreate]:
    """Parse a saved Gemini conversation page.

    Uses the same CSS selectors as the Chrome extension (gemini.js):
    - div.conversation-container for turns
    - user-query .query-text p.query-text-line for user prompts
    - model-response .markdown.markdown-main-panel for responses
    """
    cleaned = _clean_html(html)
    soup = BeautifulSoup(cleaned, "html.parser")

    turns = soup.find_all("div", class_="conversation-container")
    if not turns:
        logger.warning("No conversation-container elements found in Gemini HTML")
        return parse_generic_html(html, source_url, notes)

    parts = []
    first_prompt = None

    for turn in turns:
        # User prompt
        query_el = turn.find("user-query")
        if query_el:
            lines = query_el.select(".query-text p.query-text-line")
            if lines:
                text = "\n".join(p.get_text(strip=True) for p in lines)
                if text:
                    parts.append(f"**User:** {text}")
                    if not first_prompt:
                        first_prompt = text

        # Model response
        response_el = turn.find("model-response")
        if response_el:
            md_el = response_el.select_one(".markdown.markdown-main-panel")
            if md_el:
                text = md_el.get_text(separator="\n", strip=True)
                if text:
                    parts.append(f"**Gemini:** {text}")

    if not parts:
        logger.warning("No turns extracted from Gemini HTML")
        return parse_generic_html(html, source_url, notes)

    content = "\n\n".join(parts)
    if first_prompt:
        title = first_prompt[:80] + ("..." if len(first_prompt) > 80 else "")
    else:
        title = "Untitled"
    turn_count = len(turns)

    # Generate stable source_id
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    source_id = source_url or f"gemini-html-{content_hash}"

    metadata: dict = {
        "source": "html_import",
        "turn_count": turn_count,
    }
    if source_url:
        metadata["source_url"] = source_url
    if notes:
        metadata["notes"] = notes

    return [
        ItemCreate(
            source_type="gemini",
            source_id=source_id,
            url=source_url,
            title=title,
            content=content,
            is_own_content=False,
            metadata=metadata,
        )
    ]


def parse_generic_html(
    html: str,
    source_url: str | None = None,
    notes: str | None = None,
) -> list[ItemCreate]:
    """Fallback parser: extract title and body text from any HTML page."""
    soup = BeautifulSoup(html, "html.parser")

    # Strip noise
    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()

    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else "Untitled"

    body = soup.find("body")
    if body:
        content = body.get_text(separator="\n", strip=True)
    else:
        content = soup.get_text(separator="\n", strip=True)

    # Trim excessive whitespace
    content = re.sub(r"\n{3,}", "\n\n", content)

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
    source_id = source_url or f"html-{content_hash}"

    metadata: dict = {"source": "html_import"}
    if source_url:
        metadata["source_url"] = source_url
    if notes:
        metadata["notes"] = notes

    return [
        ItemCreate(
            source_type="share",
            source_id=source_id,
            url=source_url,
            title=title,
            content=content,
            is_own_content=False,
            metadata=metadata,
        )
    ]


def parse_html_page(
    html: str,
    page_type: str = "auto",
    source_url: str | None = None,
    notes: str | None = None,
) -> tuple[list[ItemCreate], str]:
    """Parse an HTML page, auto-detecting or using the specified type.

    Returns (items, detected_type).
    """
    detected = detect_html_type(html) if page_type == "auto" else page_type

    if detected == TYPE_GEMINI:
        items = parse_gemini_html(html, source_url, notes)
    elif detected == TYPE_CHATGPT:
        # Stub: fall back to generic for now
        logger.info("ChatGPT HTML parsing not yet implemented, using generic parser")
        items = parse_generic_html(html, source_url, notes)
    elif detected == TYPE_ARTICLE:
        items = parse_generic_html(html, source_url, notes)
    else:
        items = parse_generic_html(html, source_url, notes)

    return items, detected

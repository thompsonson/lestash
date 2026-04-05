"""Parse saved HTML pages into Le Stash items.

Supports auto-detection of page type (Gemini, etc.) and extraction
of structured content from the rendered DOM using BeautifulSoup.
"""

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta

from bs4 import BeautifulSoup
from lestash.models.item import ItemCreate

logger = logging.getLogger(__name__)

# Page type constants
TYPE_GEMINI = "gemini"
TYPE_GEMINI_SEARCH = "gemini-search"
TYPE_CHATGPT = "chatgpt"
TYPE_ARTICLE = "article"
TYPE_UNKNOWN = "unknown"


def _clean_html(html: str) -> str:
    """Strip script, style, and SVG elements to reduce noise before parsing."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()
    return str(soup)


def _parse_search_date(date_str: str) -> datetime | None:
    """Parse a Gemini search page date like '2 Apr', '31 Aug 2025', '30 Sept 2025'.

    Dates without a year are assumed to be in the current year.
    Returns datetime at noon UTC for day-level precision.
    """
    date_str = date_str.strip()
    if not date_str:
        return None

    now = datetime.now(UTC)

    # Handle relative dates
    if date_str.lower() == "today":
        return now.replace(hour=12, minute=0, second=0, microsecond=0)
    if date_str.lower() == "yesterday":
        return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    # Normalise abbreviated months (Sept → Sep)
    date_str = re.sub(r"\bSept\b", "Sep", date_str)

    for fmt in ("%d %b %Y", "%d %b"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.year == 1900:  # no year in format
                dt = dt.replace(year=now.year)
            return dt.replace(hour=12, tzinfo=UTC)
        except ValueError:
            continue

    logger.warning("Could not parse Gemini search date: %s", date_str)
    return None


def detect_html_type(html: str) -> str:
    """Detect the type of HTML page from its content.

    Uses lightweight string checks before full parsing.
    """
    # Gemini search/history page: conversation list with dates
    if "recent-conversations-container" in html:
        return TYPE_GEMINI_SEARCH

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


def parse_gemini_search_page(html: str) -> list[ItemCreate]:
    """Parse a Gemini search/history page into parent stub items.

    Each conversation entry becomes a stub parent item with title and date,
    ready to be filled in later when the full conversation is captured
    via the Chrome extension.
    """
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(".conversation-container")
    if not containers:
        logger.warning("No .conversation-container elements found in Gemini search HTML")
        return []

    items: list[ItemCreate] = []
    for container in containers:
        title_el = container.select_one(".title")
        date_el = container.select_one(".date")
        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        if not title:
            continue

        date_str = date_el.get_text(strip=True) if date_el else ""
        created_at = _parse_search_date(date_str)

        content_hash = hashlib.sha256(title.encode()).hexdigest()[:12]
        source_id = f"gemini-search-{content_hash}"

        items.append(
            ItemCreate(
                source_type="gemini",
                source_id=source_id,
                title=title,
                content="Gemini conversation (pending import).",
                created_at=created_at,
                is_own_content=True,
                metadata={
                    "source": "search_page_import",
                    "import_status": "stub",
                    "search_date": date_str,
                },
            )
        )

    logger.info("Parsed %d conversation stubs from Gemini search page", len(items))
    return items


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

    items: list[ItemCreate] = []
    first_prompt = None
    msg_count = 0

    # Generate stable parent source_id
    # Use a preliminary hash; will be refined after extracting content
    content_hash = hashlib.sha256(html.encode()).hexdigest()[:12]
    parent_source_id = source_url or f"gemini-html-{content_hash}"

    for turn in turns:
        turn_id = turn.get("id", "")

        # User prompt
        query_el = turn.find("user-query")
        if query_el:
            lines = query_el.select(".query-text p.query-text-line")
            if lines:
                text = "\n".join(p.get_text(strip=True) for p in lines)
                if text:
                    msg_count += 1
                    if not first_prompt:
                        first_prompt = text
                    msg_id = f"{parent_source_id}-user-{msg_count}"
                    items.append(
                        ItemCreate(
                            source_type="gemini",
                            source_id=msg_id,
                            title=None,
                            content=text,
                            author="user",
                            is_own_content=True,
                            metadata={
                                "role": "user",
                                "turn_id": turn_id,
                                "_parent_source_id": parent_source_id,
                            },
                        )
                    )

        # Model response
        response_el = turn.find("model-response")
        if response_el:
            md_el = response_el.select_one(".markdown.markdown-main-panel")
            if md_el:
                text = md_el.get_text(separator="\n", strip=True)
                if text:
                    msg_count += 1
                    msg_id = f"{parent_source_id}-model-{msg_count}"
                    items.append(
                        ItemCreate(
                            source_type="gemini",
                            source_id=msg_id,
                            title=None,
                            content=text,
                            author="model",
                            is_own_content=False,
                            metadata={
                                "role": "model",
                                "turn_id": turn_id,
                                "_parent_source_id": parent_source_id,
                            },
                        )
                    )

    if msg_count == 0:
        logger.warning("No turns extracted from Gemini HTML")
        return parse_generic_html(html, source_url, notes)

    if first_prompt:
        title = first_prompt[:80] + ("..." if len(first_prompt) > 80 else "")
    else:
        title = "Untitled"

    parent_metadata: dict = {
        "source": "html_import",
        "message_count": msg_count,
    }
    if source_url:
        parent_metadata["source_url"] = source_url
    if notes:
        parent_metadata["notes"] = notes

    summary = f"Gemini conversation with {msg_count} messages."

    # Insert parent BEFORE children
    items.insert(
        0,
        ItemCreate(
            source_type="gemini",
            source_id=parent_source_id,
            url=source_url,
            title=title,
            content=summary,
            is_own_content=True,
            metadata=parent_metadata,
        ),
    )

    return items


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

    if detected == TYPE_GEMINI_SEARCH:
        items = parse_gemini_search_page(html)
    elif detected == TYPE_GEMINI:
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

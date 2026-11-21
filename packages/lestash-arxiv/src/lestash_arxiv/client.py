"""arXiv API client wrapper."""

import re
from dataclasses import dataclass
from datetime import datetime

import arxiv


@dataclass
class Paper:
    """Represents an arXiv paper."""

    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    published: datetime
    updated: datetime
    categories: list[str]
    primary_category: str
    url: str
    pdf_url: str
    version: int | None = None

    @property
    def author_display(self) -> str:
        """Get display string for authors."""
        if len(self.authors) == 1:
            return self.authors[0]
        elif len(self.authors) == 2:
            return f"{self.authors[0]} and {self.authors[1]}"
        else:
            return f"{self.authors[0]} et al."


def extract_arxiv_id(entry_id: str) -> str:
    """Extract clean arXiv ID from entry URL.

    Examples:
        http://arxiv.org/abs/1706.03762v7 -> 1706.03762
        2301.00001v1 -> 2301.00001
        http://arxiv.org/abs/hep-th/9901001v3 -> hep-th/9901001
        http://arxiv.org/abs/math.GT/0309136v1 -> math/0309136
    """
    # An old-style id keeps its archive: hep-th/9901001 (#395)
    old_style = re.search(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", entry_id, re.I)
    if old_style:
        return canonical_id(old_style.group(1))

    # Remove URL prefix if present
    if "/" in entry_id:
        entry_id = entry_id.split("/")[-1]

    # Remove version suffix
    match = re.match(r"^(\d+\.\d+)", entry_id)
    if match:
        return match.group(1)

    return entry_id


def canonical_id(arxiv_id: str) -> str:
    """An id as arXiv keys it: an old-style archive in lower case, without a subject
    class. Addresses may carry one (math.GT/0309136), but arXiv's id is math/0309136,
    and a lookup with the subject class finds nothing."""
    if "/" not in arxiv_id:
        return arxiv_id
    archive, number = arxiv_id.split("/", 1)
    return f"{archive.partition('.')[0].lower()}/{number}"


def extract_version(entry_id: str) -> int | None:
    """Extract version number from arXiv entry ID.

    Examples:
        http://arxiv.org/abs/1706.03762v7 -> 7
        2301.00001v1 -> 1
        2301.00001 -> None
    """
    if "/" in entry_id:
        entry_id = entry_id.split("/")[-1]

    match = re.search(r"v(\d+)$", entry_id)
    if match:
        return int(match.group(1))
    return None


def paper_from_result(result: arxiv.Result) -> Paper:
    """Convert arxiv.Result to Paper dataclass."""
    return Paper(
        arxiv_id=extract_arxiv_id(result.entry_id),
        title=result.title.replace("\n", " ").strip(),
        abstract=result.summary.replace("\n", " ").strip(),
        authors=[author.name for author in result.authors],
        published=result.published,
        updated=result.updated,
        categories=result.categories,
        primary_category=result.primary_category,
        url=result.entry_id,
        pdf_url=result.pdf_url,
        version=extract_version(result.entry_id),
    )


def search_papers(query: str, max_results: int = 10) -> list[Paper]:
    """Search arXiv for papers matching query.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.

    Returns:
        List of Paper objects.
    """
    client = arxiv.Client()
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )

    results = list(client.results(search))
    return [paper_from_result(r) for r in results]


def get_paper(arxiv_id: str) -> Paper | None:
    """Get a specific paper by arXiv ID.

    Args:
        arxiv_id: arXiv paper ID (e.g., "1706.03762" or "2301.00001").

    Returns:
        Paper object or None if not found.
    """
    client = arxiv.Client()
    search = arxiv.Search(id_list=[arxiv_id])

    results = list(client.results(search))
    if results:
        return paper_from_result(results[0])
    return None


def get_papers(arxiv_ids: list[str]) -> list[Paper]:
    """Batch-fetch multiple papers by arXiv ID.

    Args:
        arxiv_ids: List of arXiv paper IDs.

    Returns:
        List of Paper objects (missing IDs are silently skipped).
    """
    if not arxiv_ids:
        return []

    client = arxiv.Client()
    search = arxiv.Search(id_list=arxiv_ids)

    return [paper_from_result(r) for r in client.results(search)]

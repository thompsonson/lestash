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
    """
    # Remove URL prefix if present
    if "/" in entry_id:
        entry_id = entry_id.split("/")[-1]

    # Remove version suffix
    match = re.match(r"^(\d+\.\d+)", entry_id)
    if match:
        return match.group(1)

    return entry_id


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

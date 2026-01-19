"""arXiv source plugin implementation."""

import json
from collections.abc import Iterator
from typing import Annotated

import typer
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_arxiv.client import Paper, get_paper, search_papers

console = Console()


def paper_to_item(paper: Paper) -> ItemCreate:
    """Convert Paper to ItemCreate."""
    return ItemCreate(
        source_type="arxiv",
        source_id=paper.arxiv_id,
        url=paper.url,
        title=paper.title,
        content=paper.abstract,
        author=paper.author_display,
        created_at=paper.published,
        is_own_content=False,
        metadata={
            "authors": paper.authors,
            "categories": paper.categories,
            "primary_category": paper.primary_category,
            "pdf_url": paper.pdf_url,
            "updated": paper.updated.isoformat() if paper.updated else None,
        },
    )


def display_paper(paper: Paper, verbose: bool = False) -> None:
    """Display paper details to console."""
    console.print(f"[bold]{paper.title}[/bold]")
    console.print(f"[dim]arXiv:{paper.arxiv_id}[/dim]")
    console.print(f"[cyan]{paper.author_display}[/cyan]")
    console.print(f"Published: {paper.published.strftime('%Y-%m-%d')}")
    console.print(f"Categories: {', '.join(paper.categories)}")
    console.print(f"URL: {paper.url}")

    if verbose:
        console.print()
        console.print("[bold]Abstract:[/bold]")
        console.print(paper.abstract)


class ArxivSource(SourcePlugin):
    """arXiv source plugin."""

    name = "arxiv"
    description = "arXiv academic papers"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with arXiv commands."""
        app = typer.Typer(help="arXiv source commands.")

        @app.command("search")
        def search_cmd(
            query: Annotated[str, typer.Argument(help="Search query")],
            limit: Annotated[int, typer.Option("--limit", "-n", help="Maximum results")] = 10,
        ) -> None:
            """Search arXiv for papers."""
            console.print(f"[dim]Searching arXiv for: {query}[/dim]")

            papers = search_papers(query, max_results=limit)

            if not papers:
                console.print("[yellow]No papers found.[/yellow]")
                return

            table = Table(show_header=True, header_style="bold")
            table.add_column("ID", style="dim")
            table.add_column("Title", max_width=50)
            table.add_column("Authors", max_width=25)
            table.add_column("Date")

            for paper in papers:
                title = paper.title[:47] + "..." if len(paper.title) > 50 else paper.title
                table.add_row(
                    paper.arxiv_id,
                    title,
                    paper.author_display,
                    paper.published.strftime("%Y-%m-%d"),
                )

            console.print(table)
            console.print("\n[dim]Use 'lestash arxiv save <id>' to save a paper.[/dim]")

        @app.command("info")
        def info_cmd(
            arxiv_id: Annotated[str, typer.Argument(help="arXiv paper ID")],
        ) -> None:
            """Show details for an arXiv paper."""
            paper = get_paper(arxiv_id)

            if not paper:
                console.print(f"[red]Paper {arxiv_id} not found.[/red]")
                raise typer.Exit(1)

            display_paper(paper, verbose=True)

        @app.command("save")
        def save_cmd(
            arxiv_id: Annotated[str, typer.Argument(help="arXiv paper ID to save")],
        ) -> None:
            """Save an arXiv paper to the knowledge base."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            paper = get_paper(arxiv_id)

            if not paper:
                console.print(f"[red]Paper {arxiv_id} not found.[/red]")
                raise typer.Exit(1)

            item = paper_to_item(paper)
            config = Config.load()

            with get_connection(config) as conn:
                metadata_json = json.dumps(item.metadata) if item.metadata else None
                conn.execute(
                    """
                    INSERT INTO items (
                        source_type, source_id, url, title, content,
                        author, created_at, is_own_content, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_type, source_id) DO UPDATE SET
                        title = excluded.title,
                        content = excluded.content,
                        metadata = excluded.metadata
                    """,
                    (
                        item.source_type,
                        item.source_id,
                        item.url,
                        item.title,
                        item.content,
                        item.author,
                        item.created_at,
                        item.is_own_content,
                        metadata_json,
                    ),
                )
                conn.commit()

            console.print(f"[green]Saved: {paper.title}[/green]")
            console.print(f"[dim]arXiv:{paper.arxiv_id}[/dim]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync arXiv papers based on configured keywords.

        If keywords are configured, fetches recent papers matching them.
        """
        keywords = config.get("keywords", [])

        if not keywords:
            return

        for keyword in keywords:
            papers = search_papers(keyword, max_results=config.get("max_per_keyword", 5))
            for paper in papers:
                yield paper_to_item(paper)

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "keywords": ["LLM", "transformer", "machine learning"],
            "max_per_keyword": 5,
        }

"""arXiv source plugin implementation."""

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Annotated

import typer
from lestash.models.item import ItemCreate, MediaCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_arxiv.client import Paper, get_paper, get_papers, search_papers

console = Console()


def paper_to_item(paper: Paper) -> ItemCreate:
    """Convert Paper to ItemCreate."""
    media: list[MediaCreate] | None = None
    if paper.pdf_url:
        media = [MediaCreate(media_type="pdf", url=paper.pdf_url)]

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
            "version": paper.version,
        },
        media=media,
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


_PAPER_ID_RE = re.compile(r"^\d{4}\.\d{4,5}$")


def _get_tracking_config(conn: sqlite3.Connection) -> dict:
    """Read tracking config from sources table."""
    cursor = conn.execute("SELECT config FROM sources WHERE source_type = 'arxiv'")
    row = cursor.fetchone()
    if row and row[0]:
        return json.loads(row[0])
    return {"queries": [], "untracked_papers": []}


def _save_tracking_config(conn: sqlite3.Connection, config: dict) -> None:
    """Write tracking config to sources table."""
    conn.execute(
        """
        INSERT INTO sources (source_type, config)
        VALUES ('arxiv', ?)
        ON CONFLICT(source_type) DO UPDATE SET config = excluded.config
        """,
        (json.dumps(config),),
    )
    conn.commit()


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

        @app.command("track")
        def track_cmd(
            value: Annotated[str, typer.Argument(help="Search query or arXiv paper ID")],
            category: Annotated[
                str | None,
                typer.Option("--category", "-c", help="arXiv category filter"),
            ] = None,
            max_results: Annotated[
                int,
                typer.Option("--max-results", "-n", help="Max results per query"),
            ] = 10,
        ) -> None:
            """Track a search query or re-enable tracking for an untracked paper."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            config = Config.load()
            is_paper_id = bool(_PAPER_ID_RE.match(value))

            with get_connection(config) as conn:
                tracking = _get_tracking_config(conn)

                if is_paper_id:
                    untracked = tracking.get("untracked_papers", [])
                    if value in untracked:
                        untracked.remove(value)
                        _save_tracking_config(conn, tracking)
                        console.print(f"[green]Re-enabled tracking for paper: {value}[/green]")
                    else:
                        # Check if paper is already in DB
                        cursor = conn.execute(
                            "SELECT title FROM items WHERE source_type = 'arxiv' AND source_id = ?",
                            (value,),
                        )
                        row = cursor.fetchone()
                        if row:
                            console.print(
                                f"[yellow]Paper {value} is already tracked"
                                " (all saved papers are tracked by default).[/yellow]"
                            )
                            return

                    # Fetch and save the paper if not already in DB
                    paper = get_paper(value)
                    if paper:
                        item = paper_to_item(paper)
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
                        console.print(f"[dim]arXiv:{value} (v{paper.version or '?'})[/dim]")
                    elif not conn.execute(
                        "SELECT 1 FROM items WHERE source_type = 'arxiv' AND source_id = ?",
                        (value,),
                    ).fetchone():
                        console.print(f"[red]Paper {value} not found on arXiv.[/red]")
                else:
                    query_entry: dict = {"query": value, "max_results": max_results}
                    if category:
                        query_entry["category"] = category

                    existing = tracking.get("queries", [])
                    if any(q["query"] == value for q in existing):
                        console.print(f"[yellow]Query '{value}' is already tracked.[/yellow]")
                        return
                    tracking.setdefault("queries", []).append(query_entry)
                    _save_tracking_config(conn, tracking)
                    cat_str = f" (category: {category})" if category else ""
                    console.print(f"[green]Tracking query: '{value}'{cat_str}[/green]")

        @app.command("tracked")
        def tracked_cmd() -> None:
            """List tracked queries and papers."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            config = Config.load()

            with get_connection(config) as conn:
                tracking = _get_tracking_config(conn)
                untracked = set(tracking.get("untracked_papers", []))

                # All saved arxiv papers minus untracked ones
                cursor = conn.execute(
                    "SELECT source_id, title, "
                    "json_extract(metadata, '$.version') as version "
                    "FROM items WHERE source_type = 'arxiv' "
                    "ORDER BY datetime(created_at) DESC"
                )
                all_papers = cursor.fetchall()

            queries = tracking.get("queries", [])
            tracked_papers = [p for p in all_papers if p["source_id"] not in untracked]
            untracked_papers = [p for p in all_papers if p["source_id"] in untracked]

            if not queries and not tracked_papers:
                console.print(
                    "[dim]Nothing tracked. Use 'lestash arxiv track'"
                    " to add queries, or save papers with"
                    " 'lestash arxiv save'.[/dim]"
                )
                return

            if queries:
                table = Table(
                    title="Tracked Queries",
                    show_header=True,
                    header_style="bold",
                )
                table.add_column("Query")
                table.add_column("Category")
                table.add_column("Max Results")
                for q in queries:
                    table.add_row(
                        q["query"],
                        q.get("category", "-"),
                        str(q.get("max_results", 10)),
                    )
                console.print(table)

            if tracked_papers:
                table = Table(
                    title="Tracked Papers (updates checked on sync)",
                    show_header=True,
                    header_style="bold",
                )
                table.add_column("arXiv ID", style="dim")
                table.add_column("Title", max_width=50)
                table.add_column("Version")
                for p in tracked_papers:
                    table.add_row(
                        p["source_id"],
                        p["title"] or "-",
                        f"v{p['version']}" if p["version"] else "-",
                    )
                console.print(table)

            if untracked_papers:
                table = Table(
                    title="Untracked Papers (updates skipped)",
                    show_header=True,
                    header_style="bold dim",
                )
                table.add_column("arXiv ID", style="dim")
                table.add_column("Title", max_width=50)
                for p in untracked_papers:
                    table.add_row(p["source_id"], p["title"] or "-")
                console.print(table)

        @app.command("untrack")
        def untrack_cmd(
            value: Annotated[
                str,
                typer.Argument(help="Search query or arXiv paper ID to remove"),
            ],
        ) -> None:
            """Stop tracking a query or paper for updates."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            config = Config.load()

            with get_connection(config) as conn:
                tracking = _get_tracking_config(conn)
                is_paper_id = bool(_PAPER_ID_RE.match(value))

                if is_paper_id:
                    untracked = tracking.get("untracked_papers", [])
                    if value in untracked:
                        console.print(f"[yellow]Paper {value} is already untracked.[/yellow]")
                        return
                    tracking.setdefault("untracked_papers", []).append(value)
                    _save_tracking_config(conn, tracking)
                    console.print(
                        f"[green]Untracked paper: {value} "
                        "(will no longer check for updates)[/green]"
                    )
                else:
                    queries = tracking.get("queries", [])
                    original_len = len(queries)
                    tracking["queries"] = [q for q in queries if q["query"] != value]
                    if len(tracking["queries"]) == original_len:
                        console.print(f"[yellow]Query '{value}' is not tracked.[/yellow]")
                        return
                    _save_tracking_config(conn, tracking)
                    console.print(f"[green]Untracked query: '{value}'[/green]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Sync arXiv papers from keywords, tracked queries, and tracked papers.

        Three phases:
        1. Legacy keywords from TOML config (backwards compatible)
        2. Tracked queries from sources.config DB column
        3. Update detection for tracked paper IDs
        """
        seen: set[str] = set()

        # Phase 1: Legacy keywords from TOML config
        keywords = config.get("keywords", [])
        for keyword in keywords:
            self.logger.info("Searching keyword: %s", keyword)
            papers = search_papers(keyword, max_results=config.get("max_per_keyword", 5))
            for paper in papers:
                if paper.arxiv_id not in seen:
                    seen.add(paper.arxiv_id)
                    yield paper_to_item(paper)

        # Phase 2+3: Tracked queries and papers from DB config
        try:
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            app_config = Config.load()
            with get_connection(app_config) as conn:
                tracking = _get_tracking_config(conn)

                # Get all saved arxiv paper IDs for update detection
                cursor = conn.execute("SELECT source_id FROM items WHERE source_type = 'arxiv'")
                all_saved_ids = [row[0] for row in cursor.fetchall()]
        except Exception:
            self.logger.warning("Could not read tracking config from DB, skipping")
            return

        # Phase 2: Tracked queries
        for query_conf in tracking.get("queries", []):
            query = query_conf["query"]
            max_results = query_conf.get("max_results", 10)
            category = query_conf.get("category")

            search_query = f"cat:{category} AND {query}" if category else query
            self.logger.info("Searching tracked query: %s", search_query)

            papers = search_papers(search_query, max_results=max_results)
            for paper in papers:
                if paper.arxiv_id not in seen:
                    seen.add(paper.arxiv_id)
                    yield paper_to_item(paper)

        # Phase 3: Update detection for all saved papers (minus untracked)
        untracked = set(tracking.get("untracked_papers", []))
        papers_to_check = [pid for pid in all_saved_ids if pid not in seen and pid not in untracked]

        if papers_to_check:
            self.logger.info("Checking %d saved papers for updates", len(papers_to_check))
            papers = get_papers(papers_to_check)
            for paper in papers:
                if paper.arxiv_id not in seen:
                    seen.add(paper.arxiv_id)
                    yield paper_to_item(paper)

    def configure(self) -> dict:
        """Interactive configuration."""
        return {
            "keywords": ["LLM", "transformer", "machine learning"],
            "max_per_keyword": 5,
        }

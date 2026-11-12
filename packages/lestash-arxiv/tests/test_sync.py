"""The arXiv sync reads its tracked queries and saved papers from the caller's database."""

from datetime import UTC, datetime
from pathlib import Path

from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database
from lestash.core.sync import SyncStore
from lestash.testing import forbid_default_config
from lestash_arxiv import ArxivSource
from lestash_arxiv import source as arxiv_source
from lestash_arxiv.client import Paper


def _paper(arxiv_id: str) -> Paper:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    return Paper(
        arxiv_id=arxiv_id,
        title=f"Paper {arxiv_id}",
        abstract="An abstract.",
        authors=["A. Author"],
        published=when,
        updated=when,
        categories=["cs.AI"],
        primary_category="cs.AI",
        url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def test_sync_uses_the_callers_database_not_the_default(tmp_path: Path, monkeypatch) -> None:
    """Tracked queries and saved papers come from app_config, not Config.load() (#231)."""
    config = Config(general=GeneralConfig(database_path=str(tmp_path / "test.db")))
    init_database(config)
    with get_connection(config) as conn:
        arxiv_source._save_tracking_config(
            conn, {"queries": [{"query": "agents", "max_results": 1}], "untracked_papers": []}
        )
        SyncStore(conn, "arxiv").run(iter([arxiv_source.paper_to_item(_paper("2401.00001"))]))

    searched: list[str] = []
    checked: list[list[str]] = []

    def search(query: str, max_results: int = 10) -> list[Paper]:
        searched.append(query)
        return [_paper("2402.00002")]

    def get(ids: list[str]) -> list[Paper]:
        checked.append(ids)
        return [_paper(i) for i in ids]

    monkeypatch.setattr(arxiv_source, "search_papers", search)
    monkeypatch.setattr(arxiv_source, "get_papers", get)
    forbid_default_config(monkeypatch, config)

    plugin = ArxivSource()
    plugin.app_config = config
    items = list(plugin.sync({}))

    assert searched == ["agents"]  # the tracked query, read from the caller's database
    assert checked == [["2401.00001"]]  # its saved paper, checked for updates
    assert {i.source_id for i in items} == {"2401.00001", "2402.00002"}
    assert not (Path.home() / ".config" / "lestash").exists()

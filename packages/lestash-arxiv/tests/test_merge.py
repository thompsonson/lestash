"""Shared arXiv PDFs and saved arXiv links become the paper's arXiv item (#398)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database
from lestash.core.pdf_enrich.runner import EnrichmentResult
from lestash.testing import forbid_default_config
from lestash_arxiv import share
from lestash_arxiv.client import Paper
from lestash_arxiv.merge import Link, merge_link, saved_links

WHEN = datetime(2026, 3, 1, tzinfo=UTC)


def _paper(arxiv_id: str = "2603.15401") -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        title="SWE-Skills-Bench",
        abstract="An abstract.",
        authors=["A. Author"],
        published=WHEN,
        updated=WHEN,
        categories=["cs.AI"],
        primary_category="cs.AI",
        url=f"http://arxiv.org/abs/{arxiv_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1",
        version=1,
    )


def _pdf(*lines: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for n, line in enumerate(lines):
        page.insert_text((72, 72 + 20 * n), line)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> Config:
    cfg = Config(general=GeneralConfig(database_path=str(tmp_path / "test.db")))
    init_database(cfg)
    forbid_default_config(monkeypatch, cfg)
    return cfg


def _meta(conn, item_id: int) -> dict:
    raw = conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# Telling an arXiv PDF


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("2603.15401v1.pdf", "2603.15401"),
        ("2603.15401.pdf", "2603.15401"),
        ("2603.15401v1 (1).pdf", "2603.15401"),
        ("Downloads/2603.15401V2.PDF", "2603.15401"),
        ("report-2603.15401.pdf", None),
    ],
)
def test_an_arxiv_file_name(filename: str, expected: str | None) -> None:
    assert share.arxiv_id_from_pdf(_pdf("No stamp"), filename) == expected


def test_the_arxiv_stamp_on_page_one() -> None:
    data = _pdf("A Paper", "arXiv:2603.15401v1  [cs.AI]  2 Apr 2026")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") == "2603.15401"


def test_an_old_style_stamp() -> None:
    data = _pdf("arXiv:hep-th/9901001v3  [hep-th]  12 Jan 1999")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") == "hep-th/9901001"


def test_a_cited_arxiv_id_is_not_a_stamp() -> None:
    """Only the stamp, with its category in brackets, names the paper itself."""
    data = _pdf("As shown in arXiv:2401.00001, attention", "is all you need.")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") is None


def test_a_file_that_is_not_a_pdf() -> None:
    assert share.arxiv_id_from_pdf(b"not a pdf", "notes.pdf") is None


def test_a_shared_pdf_is_stored_without_downloading(config: Config, monkeypatch) -> None:
    with get_connection(config) as conn:
        got = share.import_paper(conn, "2603.15401", fetch=lambda _: _paper())
    assert got is not None

    def no_download(url: str) -> bytes:
        raise AssertionError("the shared file is used")

    def enrich(item_id: int, *, config: Config, force: bool = False) -> EnrichmentResult:
        return EnrichmentResult(item_id=item_id, status="enriched")

    monkeypatch.setattr("lestash.core.pdf_enrich.runner.enrich_item", enrich)
    status = share.attach_pdf(got.item_id, config=config, data=b"%PDF-1.4", download=no_download)
    assert status == "enriched"
    with get_connection(config) as conn:
        assert share.has_source_pdf(conn, got.item_id)
        assert share.papers_without_pdf(conn) == []


def test_papers_without_pdf(config: Config) -> None:
    with get_connection(config) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content) "
            "VALUES ('arxiv', 'arxiv:2406.11233', 'Cited', '')"
        )
        conn.commit()
        assert [p[1:] for p in share.papers_without_pdf(conn)] == [("2406.11233", "Cited")]


# --------------------------------------------------------------------------- #
# Merging saved links


def _add(conn, source_type: str, source_id: str, url: str, **cols) -> int:
    meta = cols.pop("metadata", None)
    row = conn.execute(
        "INSERT INTO items (source_type, source_id, url, title, content, created_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            source_type,
            source_id,
            url,
            cols.get("title"),
            cols.get("content", ""),
            cols.get("created_at", "2026-04-02 17:54:46+00:00"),
            json.dumps(meta) if meta else None,
        ),
    ).fetchone()
    return row[0]


def test_saved_links_finds_shares_and_pages_of_arxiv_papers(config: Config) -> None:
    with get_connection(config) as conn:
        a = _add(conn, "share", "s1", "https://arxiv.org/pdf/2603.15401", title="x.pdf")
        b = _add(conn, "web", "w1", "https://arxiv.org/abs/2512.20660", title="Page")
        _add(conn, "share", "s2", "https://arxiv.org/list/cs.AI/recent")
        _add(conn, "share", "s3", "https://example.com/paper")
        _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        links = saved_links(conn)
    assert [(link.item_id, link.arxiv_id) for link in links] == [
        (a, "2603.15401"),
        (b, "2512.20660"),
    ]


def test_a_share_moves_into_a_new_paper(config: Config) -> None:
    with get_connection(config) as conn:
        share_id = _add(
            conn,
            "share",
            "share-1",
            "https://arxiv.org/pdf/2603.15401",
            title="2603.15401v1.pdf",
            content="Read for the bench\n\n---\n\nhttps://arxiv.org/pdf/2603.15401",
            metadata={
                "notes": "Read for the bench",
                "shared_text": "https://arxiv.org/pdf/2603.15401",
            },
        )
        other = _add(conn, "note", "n1", None, title="Cites it")
        child = _add(conn, "note", "n2", None, title="A thought on it")
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (share_id, child))
        conn.execute(
            "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
            "VALUES (?, ?, 'references', 'linker:url', 'confirmed')",
            (other, share_id),
        )
        conn.execute(
            "INSERT INTO activities (verb, item_id, occurred_at, source) "
            "VALUES ('saved', ?, '2026-04-02T17:54:46.000Z', 'app:share')",
            (share_id,),
        )
        (coll,) = conn.execute(
            "INSERT INTO collections (name) VALUES ('Reading') RETURNING id"
        ).fetchone()
        conn.execute(
            "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (coll, share_id)
        )
        conn.commit()

        merged = merge_link(
            conn,
            Link(share_id, "share", "2603.15401", "2603.15401v1.pdf"),
            fetch=lambda _: _paper(),
        )
        assert merged is not None and merged.created
        paper = merged.paper_id
        assert conn.execute("SELECT 1 FROM items WHERE id = ?", (share_id,)).fetchone() is None
        meta = _meta(conn, paper)
        assert meta["notes"] == "Read for the bench"
        assert "shared_text" not in meta  # only the address came with it
        assert meta["shared_title"] == "2603.15401v1.pdf"
        assert meta["merged_from"][0]["item_id"] == share_id
        assert meta["merged_from"][0]["metadata"]["notes"] == "Read for the bench"
        # The share's own time, not a new one
        acts = conn.execute(
            "SELECT occurred_at FROM activities WHERE item_id = ?", (paper,)
        ).fetchall()
        assert [a[0] for a in acts] == ["2026-04-02T17:54:46.000Z"]
        assert (
            conn.execute(
                "SELECT from_id FROM item_links WHERE to_id = ? AND kind = 'references'", (paper,)
            ).fetchone()[0]
            == other
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (child,)).fetchone()[0]
            == paper
        )
        assert conn.execute("SELECT item_id FROM collection_items").fetchone()[0] == paper


def test_a_page_moves_into_the_paper_already_held(config: Config) -> None:
    def no_fetch(_: str) -> Paper:
        raise AssertionError("a held paper is not fetched")

    with get_connection(config) as conn:
        held = _add(
            conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1", title="Dual-State"
        )
        page = _add(
            conn,
            "web",
            "https://arxiv.org/abs/2512.20660",
            "https://arxiv.org/abs/2512.20660",
            title="[2512.20660] Dual-State",
        )
        third = _add(conn, "note", "n1", None)
        for a, b in ((page, held), (held, page), (third, page), (third, held)):
            conn.execute(
                "INSERT INTO item_links (from_id, to_id, kind, origin, status) "
                "VALUES (?, ?, 'related', 'linker:vectors', 'suggested')",
                (a, b),
            )
        conn.commit()
        merged = merge_link(
            conn, Link(page, "web", "2512.20660", "[2512.20660] Dual-State"), fetch=no_fetch
        )
        assert merged is not None and merged.paper_id == held and not merged.created
        links = conn.execute("SELECT from_id, to_id FROM item_links ORDER BY from_id").fetchall()
        # No link from the paper to itself; the third item's link isn't doubled
        assert [tuple(r) for r in links] == [(third, held)]
        assert (
            conn.execute("SELECT COUNT(*) FROM activities WHERE item_id = ?", (held,)).fetchone()[0]
            == 0
        )


def test_a_paper_arxiv_does_not_have_leaves_the_link(config: Config) -> None:
    with get_connection(config) as conn:
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/9999.99999")
        conn.commit()
        assert (
            merge_link(conn, Link(link, "share", "9999.99999", None), fetch=lambda _: None) is None
        )
        assert conn.execute("SELECT 1 FROM items WHERE id = ?", (link,)).fetchone()

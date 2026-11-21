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
from lestash_arxiv.merge import Link, MergeError, merge_link, saved_links

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
    """A name counts when page 1 shows the id too, as a preprint's header does."""
    data = _pdf("Preprint 2603.15401, under review")
    assert share.arxiv_id_from_pdf(data, filename) == expected


def test_a_name_alone_is_not_trusted() -> None:
    """A file merely named like a paper isn't stored as that paper's PDF."""
    assert share.arxiv_id_from_pdf(_pdf("My holiday notes"), "2603.15401v1.pdf") is None


def test_the_stamp_wins_over_the_name() -> None:
    data = _pdf("arXiv:2401.00001v2  [cs.LG]  3 Jan 2024")
    assert share.arxiv_id_from_pdf(data, "2603.15401v1.pdf") == "2401.00001"


@pytest.mark.parametrize(
    "category", ["cond-mat.str-el", "physics.optics", "astro-ph.GA", "math-ph"]
)
def test_stamps_of_every_category(category: str) -> None:
    data = _pdf(f"arXiv:2401.00001v1  [{category}]  3 Jan 2024")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") == "2401.00001"


def test_the_arxiv_stamp_on_page_one() -> None:
    data = _pdf("A Paper", "arXiv:2603.15401v1  [cs.AI]  2 Apr 2026")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") == "2603.15401"


def test_an_old_style_stamp() -> None:
    # As arXiv prints it: no category for an old paper
    data = _pdf("arXiv:hep-th/9901001v3  10 May 1999")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") == "hep-th/9901001"


def test_a_cited_arxiv_id_is_not_a_stamp() -> None:
    """Only the stamp, with its category in brackets, names the paper itself."""
    data = _pdf("As shown in arXiv:2401.00001, attention", "is all you need.")
    assert share.arxiv_id_from_pdf(data, "paper.pdf") is None
    # In the usual reference style, with its category but no version or date
    cited = _pdf("[3] A. Vaswani. Attention. arXiv:1706.03762 [cs.CL], 2017.")
    assert share.arxiv_id_from_pdf(cited, "paper.pdf") is None


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
        assert share.papers_without_pdf(conn, config) == []


def test_papers_without_pdf(config: Config) -> None:
    with get_connection(config) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content) "
            "VALUES ('arxiv', 'arxiv:2406.11233', 'Cited', '')"
        )
        conn.commit()
        assert [p[1:] for p in share.papers_without_pdf(conn, config)] == [("2406.11233", "Cited")]


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
            config=config,
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
            conn,
            Link(page, "web", "2512.20660", "[2512.20660] Dual-State"),
            fetch=no_fetch,
            config=config,
        )
        assert merged is not None and merged.paper_id == held and not merged.created
        links = conn.execute("SELECT from_id, to_id FROM item_links ORDER BY from_id").fetchall()
        # No link from the paper to itself; the third item's link isn't doubled
        assert [tuple(r) for r in links] == [(third, held)]
        assert (
            conn.execute("SELECT COUNT(*) FROM activities WHERE item_id = ?", (held,)).fetchone()[0]
            == 0
        )


def test_a_saved_pages_text_is_not_copied(config: Config) -> None:
    with get_connection(config) as conn:
        page = _add(
            conn, "web", "w1", "https://arxiv.org/abs/2603.15401", content="The whole page " * 500
        )
        conn.commit()
        merged = merge_link(
            conn, Link(page, "web", "2603.15401", None), fetch=lambda _: _paper(), config=config
        )
        assert merged is not None
        assert "shared_text" not in _meta(conn, merged.paper_id)


def test_a_paper_filed_under_its_link_moves_up(config: Config) -> None:
    """Moving the link's children must not make the paper its own parent."""
    with get_connection(config) as conn:
        top = _add(conn, "note", "top", None)
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2512.20660")
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (top, link))
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (link, held))
        conn.commit()
        merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (held,)).fetchone()[0] == top
        )


def test_a_link_under_the_papers_own_child_does_not_loop(config: Config) -> None:
    with get_connection(config) as conn:
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        note = _add(conn, "note", "n1", None)
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2512.20660")
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (held, note))
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (note, link))
        conn.commit()
        merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (held,)).fetchone()[0] is None
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0] == held
        )


def test_a_links_media_come_across(config: Config) -> None:
    with get_connection(config) as conn:
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2603.15401")
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, url) VALUES (?, 'image', 'https://x/y.png')",
            (link,),
        )
        conn.commit()
        merged = merge_link(
            conn, Link(link, "share", "2603.15401", None), fetch=lambda _: _paper(), config=config
        )
        assert merged is not None
        kinds = conn.execute(
            "SELECT media_type FROM item_media WHERE item_id = ? ORDER BY media_type",
            (merged.paper_id,),
        ).fetchall()
        assert [k[0] for k in kinds] == ["image", "pdf"]


def test_a_link_that_is_gone(config: Config) -> None:
    with get_connection(config) as conn, pytest.raises(MergeError, match="no longer exists"):
        merge_link(
            conn, Link(99999, "share", "2603.15401", None), fetch=lambda _: _paper(), config=config
        )


def test_a_paper_arxiv_does_not_have_leaves_the_link(config: Config) -> None:
    with get_connection(config) as conn:
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/9999.99999")
        conn.commit()
        assert (
            merge_link(
                conn, Link(link, "share", "9999.99999", None), fetch=lambda _: None, config=config
            )
            is None
        )
        assert conn.execute("SELECT 1 FROM items WHERE id = ?", (link,)).fetchone()


def test_a_paper_further_down_under_its_link_does_not_loop(config: Config) -> None:
    with get_connection(config) as conn:
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2512.20660")
        note = _add(conn, "note", "n1", None)
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (link, note))
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (note, held))
        conn.commit()
        merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (held,)).fetchone()[0] is None
        )
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0] == held
        )


def test_a_stored_pdf_that_is_gone_is_fetched_again(config: Config, monkeypatch) -> None:
    with get_connection(config) as conn:
        got = share.import_paper(conn, "2603.15401", fetch=lambda _: _paper())
        assert got is not None
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, local_path) "
            "VALUES (?, 'source_pdf', 'gone/file.pdf')",
            (got.item_id,),
        )
        conn.commit()

    def enrich(item_id: int, *, config: Config, force: bool = False) -> EnrichmentResult:
        return EnrichmentResult(item_id=item_id, status="enriched")

    monkeypatch.setattr("lestash.core.pdf_enrich.runner.enrich_item", enrich)
    status = share.attach_pdf(got.item_id, config=config, download=lambda url: b"%PDF-1.4 x")
    assert status == "enriched"
    with get_connection(config) as conn:
        assert share.source_pdf_sha256(conn, config, got.item_id) is not None


def test_a_stored_blob_that_is_gone_counts_as_no_pdf(config: Config) -> None:
    """Recorded in the store, its file gone: fetch-pdfs lists the paper again."""
    with get_connection(config) as conn:
        got = share.import_paper(conn, "2603.15401", fetch=lambda _: _paper())
        assert got is not None
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, local_path, sha256) "
            "VALUES (?, 'source_pdf', 'gone/file.pdf', ?)",
            (got.item_id, "a" * 64),
        )
        conn.commit()
        assert share.source_pdf_sha256(conn, config, got.item_id) is None
        assert [p[0] for p in share.papers_without_pdf(conn, config)] == [got.item_id]


def _stored(config: Config, conn, item_id: int, rel: str, data: bytes) -> None:
    from lestash.core.database import get_media_dir

    path = get_media_dir(config) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    conn.execute(
        "INSERT INTO item_media (item_id, media_type, local_path) VALUES (?, 'source_pdf', ?)",
        (item_id, rel),
    )


@pytest.mark.parametrize(
    ("held_file", "link_file"),
    [
        (b"%PDF-1.4 as arXiv served it", b"%PDF-1.4 with my notes"),
        (b"%PDF-1.4 the paper", b"%PDF-1.4 the paper"),
        (None, b"%PDF-1.4 the paper"),
    ],
)
def test_a_link_with_a_pdf_file_of_its_own_is_kept(
    config: Config, held_file: bytes | None, link_file: bytes
) -> None:
    """Its file and what was read from it stay with it; it is tied to the paper."""
    with get_connection(config) as conn:
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2512.20660")
        if held_file is not None:
            _stored(config, conn, held, "p/paper.pdf", held_file)
        _stored(config, conn, link, "s1/paper.pdf", link_file)
        note = _add(conn, "pdf_annotation", f"item-{link}:p0:A1", None, title="Highlight")
        conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (link, note))
        conn.commit()
        merged = merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert merged is not None and merged.kept and merged.paper_id == held
        assert (
            conn.execute("SELECT parent_id FROM items WHERE id = ?", (note,)).fetchone()[0] == link
        )
        kind = conn.execute(
            "SELECT kind FROM item_links WHERE from_id = ? AND to_id = ?", (link, held)
        ).fetchone()[0]
        assert kind == "alternate_of"
        # Kept and linked: it isn't listed again
        assert saved_links(conn) == []


@pytest.mark.parametrize("local_path", [None])
def test_a_link_with_only_a_pdf_address_merges(config: Config, local_path: str | None) -> None:
    """An address only is no file of its own: the link merges, and its row is recorded
    rather than given to the paper."""
    with get_connection(config) as conn:
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        link = _add(
            conn, "share", "s1", "https://arxiv.org/abs/2512.20660", metadata={"notes": "Mine"}
        )
        _stored(config, conn, held, "p/paper.pdf", b"%PDF-1.4 the paper")
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, url, local_path) "
            "VALUES (?, 'source_pdf', 'https://arxiv.org/pdf/2512.20660', ?)",
            (link, local_path),
        )
        conn.commit()
        merged = merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert merged is not None and not merged.kept
        assert _meta(conn, held)["notes"] == "Mine"
        rows = conn.execute(
            "SELECT local_path FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (held,),
        ).fetchall()
        assert [r[0] for r in rows] == ["p/paper.pdf"]
        recorded = _meta(conn, held)["merged_from"][0]["source_pdf"]
        assert recorded == [{"url": "https://arxiv.org/pdf/2512.20660", "local_path": local_path}]


@pytest.mark.parametrize(
    "what",
    ["file gone", "version", "enricher picture", "annotation"],
)
def test_a_link_that_held_a_pdf_is_kept(config: Config, what: str) -> None:
    """What was read from a file stays with the link, even once the file is gone."""
    with get_connection(config) as conn:
        held = _add(conn, "arxiv", "2512.20660", "http://arxiv.org/abs/2512.20660v1")
        link = _add(conn, "share", "s1", "https://arxiv.org/abs/2512.20660")
        if what == "annotation":
            ann = _add(conn, "pdf_annotation", f"item-{link}:p0:A1", None)
            conn.execute("UPDATE items SET parent_id = ? WHERE id = ?", (link, ann))
        else:
            media_type, local_path, origin = {
                "file gone": ("source_pdf", "gone/paper.pdf", "sync"),
                "version": ("source_pdf_version", "old/paper.pdf", "sync"),
                "enricher picture": ("image", "s1/fig1.png", "enricher"),
            }[what]
            conn.execute(
                "INSERT INTO item_media (item_id, media_type, local_path, source_origin) "
                "VALUES (?, ?, ?, ?)",
                (link, media_type, local_path, origin),
            )
        conn.commit()
        merged = merge_link(
            conn, Link(link, "share", "2512.20660", None), fetch=lambda _: None, config=config
        )
        assert merged is not None and merged.kept and merged.paper_id == held
        assert conn.execute("SELECT 1 FROM items WHERE id = ?", (link,)).fetchone()

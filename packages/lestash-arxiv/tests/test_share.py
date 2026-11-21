"""A shared arXiv address becomes the paper's arXiv item, with its PDF (#395)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lestash.core.config import Config, GeneralConfig
from lestash.core.database import get_connection, init_database
from lestash.core.pdf_enrich.runner import EnrichmentResult
from lestash.core.sync import SyncStore
from lestash.testing import forbid_default_config
from lestash_arxiv import ArxivSource, share
from lestash_arxiv import source as arxiv_source
from lestash_arxiv.client import Paper

WHEN = datetime(2017, 2, 28, tzinfo=UTC)


def _paper(arxiv_id: str = "1602.05629") -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        title="Communication-Efficient Learning",
        abstract="We advocate federated learning.",
        authors=["H. Brendan McMahan", "Eider Moore"],
        published=WHEN,
        updated=WHEN,
        categories=["cs.LG"],
        primary_category="cs.LG",
        url=f"http://arxiv.org/abs/{arxiv_id}v4",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v4",
        version=4,
    )


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> Config:
    cfg = Config(general=GeneralConfig(database_path=str(tmp_path / "test.db")))
    init_database(cfg)
    forbid_default_config(monkeypatch, cfg)
    return cfg


def _meta(conn, item_id: int) -> dict:
    return json.loads(
        conn.execute("SELECT metadata FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    )


def _saves(conn, item_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM activities WHERE item_id = ? AND verb = 'saved'", (item_id,)
    ).fetchone()[0]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/pdf/1602.05629v4", "1602.05629"),
        ("https://arxiv.org/pdf/1602.05629v4.pdf", "1602.05629"),
        ("https://arxiv.org/abs/1602.05629", "1602.05629"),
        ("http://www.arxiv.org/abs/2401.12345v2?context=cs", "2401.12345"),
        ("https://export.arxiv.org/abs/2401.12345#top", "2401.12345"),
        ("https://arxiv.org/html/2401.12345v1/", "2401.12345"),
        ("https://arxiv.org/abs/hep-th/9901001v3", "hep-th/9901001"),
        ("https://arxiv.org/abs/math.GT/0309136", "math.GT/0309136"),
        ("  https://arxiv.org/abs/1602.05629 ", "1602.05629"),
        ("https://arxiv.org/list/cs.LG/recent", None),
        ("https://example.com/abs/1602.05629", None),
        ("https://arxiv.org.evil.com/abs/1602.05629", None),
        ("1602.05629", None),
    ],
)
def test_arxiv_id_from_url(url: str, expected: str | None) -> None:
    assert share.arxiv_id_from_url(url) == expected


def test_a_new_paper_is_saved_with_its_abstract_and_note(config: Config) -> None:
    with get_connection(config) as conn:
        got = share.import_paper(
            conn, "1602.05629", note="For the project", fetch=lambda _: _paper()
        )
        assert got is not None and got.created and not got.has_pdf
        row = conn.execute(
            "SELECT source_type, source_id, content, author FROM items WHERE id = ?", (got.item_id,)
        ).fetchone()
        assert tuple(row) == (
            "arxiv",
            "1602.05629",
            "We advocate federated learning.",
            "H. Brendan McMahan and Eider Moore",
        )
        meta = _meta(conn, got.item_id)
        assert meta["abstract"] == "We advocate federated learning."
        assert meta["notes"] == "For the project"
        assert _saves(conn, got.item_id) == 1


def test_a_held_paper_is_reused_and_keeps_both_notes(config: Config) -> None:
    def no_fetch(_: str) -> Paper:
        raise AssertionError("a held paper is not fetched again")

    with get_connection(config) as conn:
        first = share.import_paper(conn, "1602.05629", note="First", fetch=lambda _: _paper())
        again = share.import_paper(conn, "1602.05629", note="Second", parent_id=999, fetch=no_fetch)
        same = share.import_paper(conn, "1602.05629", note="Second", fetch=no_fetch)
        assert first is not None and again is not None and same is not None
        assert again.item_id == first.item_id and not again.created
        assert again.title == "Communication-Efficient Learning"
        assert _meta(conn, first.item_id)["notes"] == "First\n\nSecond"
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert conn.execute("SELECT parent_id FROM items").fetchone()[0] is None
        assert _saves(conn, first.item_id) == 3  # each share is a time on the Timeline


def test_an_unknown_paper_is_none(config: Config) -> None:
    with get_connection(config) as conn:
        assert share.import_paper(conn, "9999.99999", fetch=lambda _: None) is None
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_attach_pdf_stores_the_pdf_and_enriches(config: Config, monkeypatch) -> None:
    with get_connection(config) as conn:
        got = share.import_paper(conn, "1602.05629", fetch=lambda _: _paper())
    assert got is not None
    downloaded: list[str] = []

    def download(url: str) -> bytes:
        downloaded.append(url)
        return b"%PDF-1.4 a paper"

    def enrich(item_id: int, *, config: Config, force: bool = False) -> EnrichmentResult:
        return EnrichmentResult(item_id=item_id, status="enriched")

    monkeypatch.setattr("lestash.core.pdf_enrich.runner.enrich_item", enrich)
    assert share.attach_pdf(got.item_id, config=config, download=download) == "enriched"
    assert downloaded == ["https://arxiv.org/pdf/1602.05629v4"]
    with get_connection(config) as conn:
        assert share.has_source_pdf(conn, got.item_id)
        url = conn.execute(
            "SELECT url FROM item_media WHERE item_id = ? AND media_type = 'source_pdf'",
            (got.item_id,),
        ).fetchone()[0]
        assert url == "https://arxiv.org/pdf/1602.05629v4"
    # Held now: not downloaded again
    assert share.attach_pdf(got.item_id, config=config, download=download) == "held"
    assert len(downloaded) == 1


class _Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, n: int) -> bytes:
        return self.data[:n]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_download_refuses_what_is_not_a_pdf(monkeypatch) -> None:
    monkeypatch.setattr(share.urllib.request, "urlopen", lambda *a, **k: _Response(b"<html>"))
    with pytest.raises(ValueError, match="not return a PDF"):
        share.download_pdf("https://arxiv.org/pdf/1602.05629")


def test_download_refuses_a_pdf_too_large(monkeypatch) -> None:
    monkeypatch.setattr(share, "PDF_LIMIT", 8)
    monkeypatch.setattr(
        share.urllib.request, "urlopen", lambda *a, **k: _Response(b"%PDF-1.4 more")
    )
    with pytest.raises(ValueError, match="larger than"):
        share.download_pdf("https://arxiv.org/pdf/1602.05629")


def test_sync_keeps_a_held_papers_pdf_text_and_notes(config: Config, monkeypatch) -> None:
    """The sync re-saves held papers with their abstract; the PDF's text must stay."""
    with get_connection(config) as conn:
        with_pdf = share.import_paper(conn, "1602.05629", note="Mine", fetch=lambda _: _paper())
        plain = share.import_paper(conn, "2401.00001", fetch=lambda i: _paper(i))
        assert with_pdf is not None and plain is not None
        meta = _meta(conn, with_pdf.item_id) | {"pdf_sha256": "abc", "extractor_version": 4}
        conn.execute(
            "UPDATE items SET content = 'The whole paper.', metadata = ? WHERE id = ?",
            (json.dumps(meta), with_pdf.item_id),
        )
        conn.execute(
            "INSERT INTO item_media (item_id, media_type, local_path) "
            "VALUES (?, 'source_pdf', 'x.pdf')",
            (with_pdf.item_id,),
        )
        conn.execute("UPDATE items SET content = 'Old abstract.' WHERE id = ?", (plain.item_id,))
        conn.commit()

    newer = _paper()
    newer.title = "Communication-Efficient Learning (v5)"
    monkeypatch.setattr(
        arxiv_source,
        "get_papers",
        lambda ids: [newer if i == "1602.05629" else _paper(i) for i in ids],
    )
    plugin = ArxivSource()
    plugin.app_config = config
    with get_connection(config) as conn:
        SyncStore(conn, "arxiv").run(plugin.sync({}))
        row = conn.execute(
            "SELECT title, content FROM items WHERE id = ?", (with_pdf.item_id,)
        ).fetchone()
        assert tuple(row) == ("Communication-Efficient Learning (v5)", "The whole paper.")
        meta = _meta(conn, with_pdf.item_id)
        assert meta["notes"] == "Mine" and meta["pdf_sha256"] == "abc" and meta["abstract"]
        # A paper without its PDF still takes arXiv's abstract
        text = conn.execute("SELECT content FROM items WHERE id = ?", (plain.item_id,)).fetchone()[
            0
        ]
        assert text == "We advocate federated learning."


def test_a_paper_keyed_with_the_old_prefix_is_found(config: Config) -> None:
    with get_connection(config) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content) "
            "VALUES ('arxiv', 'arxiv:2406.11233', 'Cited in the thesis', '')"
        )
        conn.commit()
        got = share.import_paper(conn, "2406.11233", fetch=lambda _: _paper("2406.11233"))
        assert got is not None and not got.created and got.title == "Cited in the thesis"
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert share.pdf_url_of(conn, got.item_id) == "https://arxiv.org/pdf/2406.11233"


def test_an_old_style_paper_is_found_when_shared_again(config: Config) -> None:
    """The id from the address matches the id the paper is stored under."""
    from lestash_arxiv.client import extract_arxiv_id

    old = _paper("hep-th/9901001")
    old.arxiv_id = extract_arxiv_id("http://arxiv.org/abs/hep-th/9901001v3")
    with get_connection(config) as conn:
        first = share.import_paper(conn, "hep-th/9901001", fetch=lambda _: old)
        assert first is not None and first.created
        arxiv_id = share.arxiv_id_from_url("https://arxiv.org/abs/hep-th/9901001v3")
        again = share.import_paper(conn, arxiv_id or "", fetch=lambda _: None)
        assert again is not None and again.item_id == first.item_id and not again.created


def test_what_came_with_the_share_is_kept(config: Config) -> None:
    with get_connection(config) as conn:
        got = share.import_paper(
            conn,
            "1602.05629",
            shared={
                "shared_text": "Look at this https://arxiv.org/abs/1602.05629",
                "shared_title": None,
            },
            fetch=lambda _: _paper(),
        )
        assert got is not None
        meta = _meta(conn, got.item_id)
        assert meta["shared_text"] == "Look at this https://arxiv.org/abs/1602.05629"
        assert "shared_title" not in meta
        share.import_paper(
            conn, "1602.05629", shared={"shared_title": "My title"}, fetch=lambda _: None
        )
        assert _meta(conn, got.item_id)["shared_title"] == "My title"


def test_sync_reads_each_held_paper_when_it_is_yielded(config: Config, monkeypatch) -> None:
    """A note added while the sync runs isn't overwritten by an older reading."""
    with get_connection(config) as conn:
        for arxiv_id in ("2401.00001", "2401.00002"):
            share.import_paper(conn, arxiv_id, fetch=lambda i: _paper(i))
    monkeypatch.setattr(arxiv_source, "get_papers", lambda ids: [_paper(i) for i in ids])
    plugin = ArxivSource()
    plugin.app_config = config
    items = plugin.sync({})
    next(items)
    with get_connection(config) as conn:
        conn.execute(
            "UPDATE items SET metadata = json_set(metadata, '$.notes', 'Added during the sync') "
            "WHERE source_id = '2401.00002'"
        )
        conn.commit()
    second = next(items)
    assert second.source_id == "2401.00002"
    assert (second.metadata or {})["notes"] == "Added during the sync"


def test_an_old_style_id_is_read_in_arxivs_case() -> None:
    assert share.arxiv_id_from_url("https://arxiv.org/abs/HEP-TH/9901001") == "hep-th/9901001"
    assert share.arxiv_id_from_url("https://arxiv.org/abs/MATH.gt/0309136") == "math.GT/0309136"


def test_a_paper_stored_under_a_bare_number_is_rekeyed(config: Config) -> None:
    """Papers stored before #397 lost their archive; they take it back, not a twin."""
    with get_connection(config) as conn:
        conn.execute(
            "INSERT INTO items (source_type, source_id, url, title, content) VALUES "
            "('arxiv', '9901001v3', 'http://arxiv.org/abs/hep-th/9901001v3', 'Old', '')"
        )
        conn.commit()
        got = share.import_paper(conn, "hep-th/9901001", fetch=lambda _: None)
        assert got is not None and not got.created and got.title == "Old"
        assert [r[0] for r in conn.execute("SELECT source_id FROM items")] == ["hep-th/9901001"]


def test_a_held_paper_added_under_an_item_keeps_its_notes(config: Config) -> None:
    """+ Add → New link saves the note on the link; the held paper isn't given it too."""
    with get_connection(config) as conn:
        first = share.import_paper(conn, "1602.05629", note="Mine", fetch=lambda _: _paper())
        assert first is not None
        share.import_paper(
            conn, "1602.05629", note="On the link", shared={"shared_title": "T"}, parent_id=3
        )
        meta = _meta(conn, first.item_id)
        assert meta["notes"] == "Mine" and "shared_title" not in meta

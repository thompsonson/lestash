"""POST /api/arxiv/import-paper: a shared arXiv link becomes the paper, with its PDF (#395)."""

from lestash_arxiv import share
from lestash_arxiv.share import Imported


def test_a_new_paper_is_saved_and_its_pdf_fetched(client, monkeypatch):
    calls: list[tuple] = []

    def save(conn, arxiv_id, *, note=None, shared=None, parent_id=None):
        calls.append(("save", arxiv_id, note, parent_id))
        return Imported(41, "A paper", created=True, has_pdf=False)

    def attach(item_id, *, config):
        calls.append(("pdf", item_id))
        return "enriched"

    monkeypatch.setattr(share, "import_paper", save)
    monkeypatch.setattr(share, "attach_pdf", attach)
    response = client.post(
        "/api/arxiv/import-paper",
        json={"url": "https://arxiv.org/pdf/1602.05629v4", "note": "Useful", "parent_id": 7},
    )
    assert response.status_code == 200
    assert response.json() == {
        "item_id": 41,
        "title": "A paper",
        "created": True,
        "fetching_pdf": True,
    }
    # The test client runs the background step before returning
    assert calls == [("save", "1602.05629", "Useful", 7), ("pdf", 41)]


def test_a_held_paper_with_its_pdf_is_not_fetched_again(client, monkeypatch):
    monkeypatch.setattr(
        share, "import_paper", lambda conn, i, **k: Imported(5, "Held", created=False, has_pdf=True)
    )

    def attach(item_id, *, config):
        raise AssertionError("not fetched again")

    monkeypatch.setattr(share, "attach_pdf", attach)
    response = client.post(
        "/api/arxiv/import-paper", json={"url": "https://arxiv.org/abs/1602.05629"}
    )
    assert response.json() == {
        "item_id": 5,
        "title": "Held",
        "created": False,
        "fetching_pdf": False,
    }


def test_a_failed_pdf_leaves_the_paper_saved(client, monkeypatch):
    monkeypatch.setattr(
        share, "import_paper", lambda conn, i, **k: Imported(5, "P", created=True, has_pdf=False)
    )

    def attach(item_id, *, config):
        raise OSError("network down")

    monkeypatch.setattr(share, "attach_pdf", attach)
    response = client.post(
        "/api/arxiv/import-paper", json={"url": "https://arxiv.org/abs/1602.05629"}
    )
    assert response.status_code == 200


def test_not_an_arxiv_address(client):
    response = client.post("/api/arxiv/import-paper", json={"url": "https://example.com/paper.pdf"})
    assert response.status_code == 400


def test_a_paper_arxiv_does_not_have(client, monkeypatch):
    monkeypatch.setattr(share, "import_paper", lambda conn, i, **k: None)
    response = client.post(
        "/api/arxiv/import-paper", json={"url": "https://arxiv.org/abs/9999.99999"}
    )
    assert response.status_code == 404


def test_arxiv_unreachable(client, monkeypatch):
    def fail(conn, i, **k):
        raise OSError("timed out")

    monkeypatch.setattr(share, "import_paper", fail)
    response = client.post(
        "/api/arxiv/import-paper", json={"url": "https://arxiv.org/abs/1602.05629"}
    )
    assert response.status_code == 502


def test_the_shared_text_and_title_reach_the_paper(client, monkeypatch):
    seen: dict = {}

    def save(conn, arxiv_id, *, note=None, shared=None, parent_id=None):
        seen.update(shared or {})
        return Imported(1, "P", created=True, has_pdf=True)

    monkeypatch.setattr(share, "import_paper", save)
    client.post(
        "/api/arxiv/import-paper",
        json={"url": "https://arxiv.org/abs/1602.05629", "shared_text": "t", "shared_title": "T"},
    )
    assert seen == {"shared_text": "t", "shared_title": "T"}


# A shared arXiv PDF file becomes the paper (#398)


def _upload(client, name: str = "2603.15401v1.pdf"):
    import io

    return client.post(
        "/api/import", files={"file": (name, io.BytesIO(b"%PDF-1.4 a paper"), "application/pdf")}
    )


def test_a_shared_arxiv_pdf_becomes_the_paper(client, monkeypatch):
    calls: list[tuple] = []

    def save(conn, arxiv_id, *, parent_id=None, **k):
        calls.append(("save", arxiv_id, parent_id))
        return Imported(41, "A paper", created=True, has_pdf=False)

    def attach(item_id, *, config, data=None):
        calls.append(("pdf", item_id, data))
        return "enriched"

    monkeypatch.setattr(share, "import_paper", save)
    monkeypatch.setattr(share, "attach_pdf", attach)
    response = _upload(client)
    assert response.status_code == 200
    body = response.json()
    assert (body["source_type"], body["item_id"], body["items_added"]) == ("arxiv", 41, 1)
    # The shared file is the source PDF: not downloaded again
    assert calls == [("save", "2603.15401", None), ("pdf", 41, b"%PDF-1.4 a paper")]


def test_a_held_paper_with_its_pdf_is_returned(client, monkeypatch):
    monkeypatch.setattr(
        share, "import_paper", lambda conn, i, **k: Imported(5, "Held", created=False, has_pdf=True)
    )

    def attach(item_id, *, config, data=None):
        raise AssertionError("not stored again")

    monkeypatch.setattr(share, "attach_pdf", attach)
    body = _upload(client).json()
    assert (body["item_id"], body["items_added"], body["items_held"]) == (5, 0, 1)


def test_an_arxiv_pdf_stays_a_pdf_when_arxiv_cannot_be_reached(client, monkeypatch):
    from unittest.mock import patch

    def fail(conn, i, **k):
        raise OSError("HTTP 429")

    monkeypatch.setattr(share, "import_paper", fail)
    with patch("lestash.core.text_extract.convert_to_markdown", return_value="# Paper"):
        body = _upload(client).json()
    assert body["source_type"] == "pdf" and body["items_added"] == 1


def test_a_pdf_that_is_not_from_arxiv_is_a_pdf(client, monkeypatch):
    from unittest.mock import patch

    def save(conn, i, **k):
        raise AssertionError("not an arXiv paper")

    monkeypatch.setattr(share, "import_paper", save)
    with patch("lestash.core.text_extract.convert_to_markdown", return_value="# Report"):
        body = _upload(client, "report.pdf").json()
    assert body["source_type"] == "pdf"

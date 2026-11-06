"""Tests for the Jelu source plugin."""

import httpx
import pytest
from lestash_jelu import client as jelu_client
from lestash_jelu.client import JeluClient, load_credentials, save_credentials
from lestash_jelu.source import JeluSource, status_label, sync_items

from tests.conftest import BASE, make_handler


def _by_id(items):
    return {i.source_id: i for i in items}


def test_sync_items_books_then_children(client) -> None:
    items = list(sync_items(client))
    ids = [i.source_id for i in items]
    # Books come before any child so children can link to them during sync
    assert ids[:2] == ["jelu:book:b-1", "jelu:book:b-2"]
    assert set(ids[2:]) == {"jelu:notes:ub-1", "jelu:review:r-1", "jelu:quote:q-1"}


def test_book_item(client) -> None:
    book = _by_id(sync_items(client))["jelu:book:b-1"]
    assert book.source_type == "jelu"
    assert book.title == "The Alignment Problem"
    assert book.author == "Brian Christian"
    assert book.url == f"{BASE}/books/ub-1"
    assert "Status: Finished (2026-02-20)" in book.content
    assert "Machine learning and human values." in book.content  # HTML stripped
    assert book.metadata["isbn13"] == "9780393868333"
    assert book.metadata["asin"] == "B085T55LGK"
    assert book.metadata["tags"] == ["ai", "nonfiction"]
    assert book.metadata["cover_url"] == f"{BASE}/files/alignment.jpg"
    assert book.created_at is not None and book.created_at.year == 2026
    assert book.is_own_content is False


def test_children_point_at_their_book(client) -> None:
    items = _by_id(sync_items(client))
    review = items["jelu:review:r-1"]
    assert review.title == "Review of The Alignment Problem (4.5/5)"
    assert review.content == "Great on RL."
    assert review.is_own_content is True
    assert review.metadata["_parent_source_id"] == "jelu:book:b-1"

    notes = items["jelu:notes:ub-1"]
    assert notes.content == "Watkins and TD learning."
    assert notes.metadata["_parent_source_id"] == "jelu:book:b-1"

    quote = items["jelu:quote:q-1"]
    assert quote.title == "Quote from The Alignment Problem (ch. 4)"
    assert quote.metadata["_parent_source_id"] == "jelu:book:b-1"


def test_to_read_book_without_notes(client) -> None:
    items = _by_id(sync_items(client))
    assert "Status: To read" in items["jelu:book:b-2"].content
    assert "jelu:notes:ub-2" not in items


@pytest.mark.parametrize(
    ("userbook", "label"),
    [
        ({"lastReadingEvent": "CURRENTLY_READING"}, "Reading"),
        ({"lastReadingEvent": "DROPPED"}, "Dropped"),
        ({"lastReadingEvent": None, "toRead": True}, "To read"),
        ({}, "In library"),
    ],
)
def test_status_label(userbook, label) -> None:
    assert status_label(userbook) == label


def test_pagination_reads_every_page(monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "PAGE_SIZE", 2)
    many = [{"id": f"ub-{n}", "book": {"id": f"b-{n}", "title": f"Book {n}"}} for n in range(5)]
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(make_handler(userbooks=many, calls=calls))
    with JeluClient(BASE, "secret", transport=transport) as c:
        assert len(list(c.userbooks())) == 5
    pages = [r.url.params["page"] for r in calls if r.url.path == "/api/v1/userbooks"]
    assert pages == ["0", "1", "2"]


def test_bad_token_raises() -> None:
    transport = httpx.MockTransport(make_handler())
    with JeluClient(BASE, "wrong", transport=transport) as c, pytest.raises(httpx.HTTPStatusError):
        c.me()


def test_credentials_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    assert load_credentials() is None
    save_credentials("https://jelu.test/", "secret")
    assert load_credentials() == ("https://jelu.test", "secret")
    assert (tmp_path / "jelu.json").stat().st_mode & 0o777 == 0o600


def test_sync_without_credentials_yields_nothing(monkeypatch) -> None:
    monkeypatch.setattr("lestash_jelu.source.load_credentials", lambda: None)
    assert list(JeluSource().sync({})) == []

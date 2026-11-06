"""Tests for the Jelu source plugin, against a fake Jelu with Jelu's auth model."""

import httpx
import pytest
from lestash_jelu import client as jelu_client
from lestash_jelu import source as jelu_source
from lestash_jelu.client import JeluAuthError, JeluClient, load_credentials, save_credentials
from lestash_jelu.source import JeluSource, status_label, sync_items

from tests.conftest import BASE, REVIEWS, FakeJelu


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
    assert "Machine learning & human values." in book.content  # HTML stripped, entities decoded
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


def test_review_of_a_book_outside_the_library_has_no_parent() -> None:
    reviews = [
        *REVIEWS,
        {"id": "r-2", "book": "b-gone", "user": "u-1", "text": "Meh.", "rating": 4.0},
    ]
    jelu = FakeJelu(reviews=reviews)
    with JeluClient(BASE, "mt", "pw", transport=httpx.MockTransport(jelu)) as c:
        review = _by_id(sync_items(c))["jelu:review:r-2"]
    assert "_parent_source_id" not in review.metadata
    assert review.metadata["jelu_book_id"] == "b-gone"
    assert review.title == "Review of a book (2/5)"


def test_private_reviews_need_the_session(jelu, client) -> None:
    """The private review only comes back because the client sends its session token."""
    assert REVIEWS[0]["visibility"] == "PRIVATE"
    assert "jelu:review:r-1" in _by_id(sync_items(client))
    assert all("X-Auth-Token" in r.headers for r in jelu.calls if r.url.path == "/api/v1/reviews")


def test_logs_in_with_basic_auth_once_then_uses_the_session(jelu, client) -> None:
    list(sync_items(client))
    basic = [r for r in jelu.calls if r.headers.get("Authorization", "").startswith("Basic ")]
    assert [r.url.path for r in basic] == ["/api/v1/users/me"]
    assert not any(r.headers.get("Authorization", "").startswith("Bearer ") for r in jelu.calls)
    assert jelu.logins == 1


def test_expired_session_logs_in_again(jelu) -> None:
    with JeluClient(BASE, "mt", "pw", token="stale", transport=httpx.MockTransport(jelu)) as c:
        items = _by_id(sync_items(c))
        # me() caught the stale token, so private reviews still came back
        assert "jelu:review:r-1" in items
        assert c.token == "sess-1"
        jelu.expire_sessions()
        assert len(list(c.userbooks())) == 2
    assert jelu.logins == 2


def test_wrong_password_raises() -> None:
    transport = httpx.MockTransport(FakeJelu())
    with JeluClient(BASE, "mt", "wrong", transport=transport) as c, pytest.raises(JeluAuthError):
        c.login()


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


def test_pagination_reads_every_page_with_a_stable_sort(monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "PAGE_SIZE", 2)
    many = [{"id": f"ub-{n}", "book": {"id": f"b-{n}", "title": f"Book {n}"}} for n in range(5)]
    jelu = FakeJelu(userbooks=many)
    with JeluClient(BASE, "mt", "pw", transport=httpx.MockTransport(jelu)) as c:
        assert len(list(c.userbooks())) == 5
    pages = [r.url.params for r in jelu.calls if r.url.path == "/api/v1/userbooks"]
    assert [p["page"] for p in pages] == ["0", "1", "2"]
    assert {p["sort"] for p in pages} == {"id,asc"}


def test_unknown_page_format_reads_until_empty() -> None:
    pages = [{"content": [{"id": 1}]}, {"content": [{"id": 2}]}, {"content": []}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me":
            return httpx.Response(200, json={"user": {"id": "u-1"}, "token": "t"})
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    with JeluClient(BASE, "mt", "pw", transport=httpx.MockTransport(handler)) as c:
        assert [b["id"] for b in c.userbooks()] == [1, 2]


def test_credentials_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    assert load_credentials() is None
    save_credentials("https://jelu.test/", "mt", "pw", "sess-1")
    assert load_credentials() == {
        "url": "https://jelu.test",
        "username": "mt",
        "password": "pw",
        "token": "sess-1",
    }
    assert (tmp_path / "jelu.json").stat().st_mode & 0o777 == 0o600


def test_sync_saves_the_new_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    save_credentials(BASE, "mt", "pw")
    jelu = FakeJelu()
    real = jelu_source.JeluClient
    monkeypatch.setattr(
        jelu_source,
        "JeluClient",
        lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(jelu)),
    )
    assert len(list(JeluSource().sync({}))) == 5
    assert load_credentials()["token"] == "sess-1"
    # The next sync reuses it rather than logging in again
    list(JeluSource().sync({}))
    assert jelu.logins == 1


def test_sync_without_credentials_warns_and_yields_nothing(monkeypatch, caplog) -> None:
    monkeypatch.setattr("lestash_jelu.source.load_credentials", lambda: None)
    with caplog.at_level("WARNING"):
        assert list(JeluSource().sync({})) == []
    assert "not configured" in caplog.text

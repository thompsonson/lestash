"""Tests for the Jelu source plugin, against a fake Jelu with Jelu's auth model."""

import os

import httpx
import pytest
from lestash_jelu import client as jelu_client
from lestash_jelu import source as jelu_source
from lestash_jelu.client import JeluAuthError, JeluClient, load_credentials, save_credentials
from lestash_jelu.source import JeluSource, status_label, sync_items
from typer.testing import CliRunner

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


def _serve_pages(pages, calls=None):
    """A Jelu that serves the given page bodies by page number, after a session login."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me":
            return httpx.Response(200, json={"user": {"id": "u-1"}, "token": "t"})
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json=pages(int(request.url.params["page"])))

    return httpx.MockTransport(handler)


def test_pages_with_last_but_no_total_pages_are_all_read() -> None:
    bodies = [
        {"content": [{"id": 1}], "last": False},
        {"content": [{"id": 2}], "last": False},
        {"content": [{"id": 3}], "last": True},
    ]
    with JeluClient(BASE, "mt", "pw", transport=_serve_pages(bodies.__getitem__)) as c:
        assert [b["id"] for b in c.userbooks()] == [1, 2, 3]


def test_unknown_page_format_that_never_ends_stops(monkeypatch) -> None:
    """A server that ignores ?page= and has no last/totalPages must not loop forever."""
    monkeypatch.setattr(jelu_client, "MAX_PAGES", 5, raising=False)
    calls: list[httpx.Request] = []

    def same_page(page: int):
        if page > 50:
            raise AssertionError("still paging after 50 pages")
        return {"content": [{"id": 1}]}

    with (
        JeluClient(BASE, "mt", "pw", transport=_serve_pages(same_page, calls)) as c,
        pytest.raises(RuntimeError, match="more than 5 pages"),
    ):
        list(c.userbooks())
    assert len(calls) == 5


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


def test_saving_over_a_world_readable_file_never_exposes_the_password(
    tmp_path, monkeypatch
) -> None:
    """An old 0644 jelu.json must not hold the password, even mid-write."""
    path = tmp_path / "jelu.json"
    path.write_text("{}")
    path.chmod(0o644)
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: path)
    real_dump = jelu_client.json.dump

    def checking_dump(data, fh, **kwargs):
        # Whatever file the password goes into must already be private
        assert os.fstat(fh.fileno()).st_mode & 0o077 == 0
        real_dump(data, fh, **kwargs)

    monkeypatch.setattr(jelu_client.json, "dump", checking_dump)
    save_credentials(BASE, "mt", "pw", "sess-1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_credentials()["password"] == "pw"
    assert [p.name for p in tmp_path.iterdir()] == ["jelu.json"]  # no temp file left


def test_a_failed_save_keeps_the_old_credentials(tmp_path, monkeypatch) -> None:
    """A crash mid-write must not leave an empty or half-written jelu.json."""
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    save_credentials(BASE, "mt", "pw", "sess-1")

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jelu_client.json, "dump", crash)
    with pytest.raises(OSError, match="disk full"):
        save_credentials(BASE, "mt", "pw", "sess-2")
    assert load_credentials()["token"] == "sess-1"
    assert [p.name for p in tmp_path.iterdir()] == ["jelu.json"]


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


def _fake_jelu_client(monkeypatch, jelu: FakeJelu) -> None:
    real = jelu_source.JeluClient
    monkeypatch.setattr(
        jelu_source,
        "JeluClient",
        lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(jelu)),
    )


def test_auth_prompts_for_the_password(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    _fake_jelu_client(monkeypatch, FakeJelu())
    app = JeluSource().get_commands()
    result = CliRunner().invoke(app, ["auth", "--url", BASE, "-u", "mt"], input="pw\n")
    assert result.exit_code == 0, result.output
    assert "pw" not in result.output  # hidden input
    assert load_credentials()["password"] == "pw"


def test_auth_has_no_password_option(tmp_path, monkeypatch) -> None:
    """A password on the command line lands in shell history and ps output."""
    monkeypatch.setattr(jelu_client, "get_config_path", lambda: tmp_path / "jelu.json")
    _fake_jelu_client(monkeypatch, FakeJelu())
    app = JeluSource().get_commands()
    result = CliRunner().invoke(app, ["auth", "--url", BASE, "-u", "mt", "--password", "pw"])
    assert result.exit_code == 2
    assert "No such option: --password" in result.output
    assert load_credentials() is None


def test_sync_without_credentials_warns_and_yields_nothing(monkeypatch, caplog) -> None:
    monkeypatch.setattr("lestash_jelu.source.load_credentials", lambda: None)
    with caplog.at_level("WARNING"):
        assert list(JeluSource().sync({})) == []
    assert "not configured" in caplog.text

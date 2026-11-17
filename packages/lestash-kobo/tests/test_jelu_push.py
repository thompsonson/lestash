"""The Kobo's reading status into Jelu: ``lestash kobo push-jelu`` (#203)."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from lestash.core.database import get_connection
from lestash.core.sync import SyncStore
from lestash_kobo import KoboSource
from lestash_kobo import jelu_push as push
from lestash_kobo.source import _store
from typer.testing import CliRunner

from tests.conftest import DRIVE_PDF, NN


class FakeJelu:
    """Jelu 0.88's behaviour for the calls push-jelu makes."""

    def __init__(self) -> None:
        self.books_by_id: dict[str, dict] = {}
        self.userbooks_by_book: dict[str, dict] = {}
        self.events: list[tuple[str, str, str | None]] = []
        self.created: list[dict] = []
        self.token = "t"
        self.url = "https://jelu.test"

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass

    def add(self, title, authors, isbn13=None, last_event=None, in_library=True) -> str:
        book_id = f"b{len(self.books_by_id) + 1}"
        self.books_by_id[book_id] = {
            "id": book_id,
            "title": title,
            "authors": [{"name": a} for a in authors],
            "isbn13": isbn13,
        }
        if in_library:
            self.userbooks_by_book[book_id] = {
                "id": f"u{book_id}",
                "book": self.books_by_id[book_id],
                "lastReadingEvent": last_event,
            }
        return book_id

    def userbooks(self):
        return iter(list(self.userbooks_by_book.values()))

    def books(self):
        return iter(list(self.books_by_id.values()))

    def add_book(self, title, authors, isbn10=None, isbn13=None, publisher=None):
        book_id = self.add(title, authors, isbn13, in_library=False)
        self.created.append(self.books_by_id[book_id])
        return self.books_by_id[book_id]

    def add_reading_event(self, book_id, event_type, when=None):
        self.events.append((book_id, event_type, when))
        userbook = self.userbooks_by_book.setdefault(
            book_id, {"id": f"u{book_id}", "book": self.books_by_id[book_id]}
        )
        userbook["lastReadingEvent"] = event_type
        return {"id": f"e{len(self.events)}"}


def _title(**kw) -> push.KoboTitle:
    base = dict(
        content_id="c1",
        title="Neural Networks for Beginners",
        authors=["Brian Murray"],
        isbn13="9798224814305",
        status="reading",
        last_read=datetime(2024, 10, 4, 2, 7, 29, tzinfo=UTC),
        kind="store",
    )
    return push.KoboTitle(**{**base, **kw})


def _plan(test_db, jelu: FakeJelu, *titles: push.KoboTitle, also=None) -> push.Plan:
    books = push.jelu_books(jelu.userbooks(), jelu.books())
    with get_connection(test_db) as conn:
        return push.plan(conn, list(titles), books, also)


# --- reading the Kobo ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("urn:isbn:9781633437166", "9781633437166"),
        ("978-1-63343-716-6", "9781633437166"),
        ("0306406152", "9780306406157"),  # ISBN-10 → 13
        ("9781633437167", None),  # a wrong check digit
        ("", None),
        (None, None),
        ("not an isbn", None),
    ],
)
def test_isbns_are_compared_as_13_digits(raw, expected) -> None:
    assert push.isbn13(raw) == expected


def test_authors_are_split_and_unquoted() -> None:
    assert push.split_authors('"David I. Spivak, Robert E. Kent"') == [
        "David I. Spivak",
        "Robert E. Kent",
    ]
    assert push.split_authors("Ann Lee & Bo Chen") == ["Ann Lee", "Bo Chen"]
    assert push.split_authors("") == []


def test_only_reading_and_finished_titles_count() -> None:
    rows = [
        {"ContentID": "a", "Title": "A", "ReadStatus": 1, "MimeType": "application/epub+zip"},
        {
            "ContentID": "b",
            "Title": "B",
            "ReadStatus": 2,
            "DateLastRead": "2026-10-29T03:46:43.474",
        },
        {"ContentID": "c", "Title": "C", "ReadStatus": 0},
        {"ContentID": "file:///mnt/onboard/My Notebooks/n.nebo", "Title": "N", "ReadStatus": 1},
    ]
    titles = push.kobo_titles(rows)
    assert [(t.content_id, t.status) for t in titles] == [("a", "reading"), ("b", "finished")]
    assert titles[1].last_read == datetime(2026, 10, 29, 3, 46, 43, 474000, tzinfo=UTC)


# --- matching ---


def test_title_keys_set_aside_case_articles_and_subtitles() -> None:
    assert push.title_key("The Alignment Problem: Machine Learning and Human Values") == (
        "alignment problem"
    )
    assert push.title_key("Schrödinger’s Girl") == push.title_key("Schrodinger's girl")


def test_an_isbn_match_wins(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Some other title", ["Nobody"], isbn13="9798224814305")
    book, how = push.match(_title(), push.jelu_books(jelu.userbooks(), jelu.books()))
    assert (book.title, how) == ("Some other title", "isbn")


def test_a_title_needs_a_shared_author_to_count(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["B. Murray"])
    books = push.jelu_books(jelu.userbooks(), jelu.books())
    assert push.match(_title(isbn13=None), books)[1] == "title and author"
    assert push.match(_title(isbn13=None, authors=["Someone Else"]), books)[1] == "title only"


def test_volumes_by_one_author_are_different_books(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Dune: The Butlerian Jihad", ["Brian Herbert", "Kevin J. Anderson"])
    books = push.jelu_books(jelu.userbooks(), jelu.books())
    other = _title(isbn13=None, title="Dune: The Machine Crusade", authors=["Brian Herbert"])
    assert push.match(other, books) == (None, None)
    short = _title(isbn13=None, title="Dune", authors=["Brian Herbert"])
    assert push.match(short, books)[1] == "title only"


def test_the_whole_title_wins_over_one_missing_its_subtitle(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Dune: The Butlerian Jihad", ["Brian Herbert"])
    jelu.add("Dune", ["Frank Herbert"])
    books = push.jelu_books(jelu.userbooks(), jelu.books())
    book, how = push.match(_title(isbn13=None, title="Dune", authors=["Frank Herbert"]), books)
    assert (book.title, how) == ("Dune", "title and author")


# --- the plan ---


@pytest.mark.parametrize(
    ("kobo", "jelu", "action", "event"),
    [
        ("reading", None, push.START, push.READING),
        ("reading", "CURRENTLY_READING", push.NOTHING, None),
        ("reading", "FINISHED", push.CONFLICT, None),
        ("reading", "DROPPED", push.CONFLICT, None),
        ("finished", None, push.FINISH, push.FINISHED),
        ("finished", "CURRENTLY_READING", push.FINISH, push.FINISHED),
        ("finished", "FINISHED", push.NOTHING, None),
        ("finished", "DROPPED", push.CONFLICT, None),
    ],
)
def test_a_status_only_moves_forward(test_db, kobo, jelu, action, event) -> None:
    fake = FakeJelu()
    fake.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305", jelu)
    (step,) = _plan(test_db, fake, _title(status=kobo)).steps
    assert (step.action, step.event) == (action, event)


def test_a_book_jelu_lacks_is_created_only_with_an_isbn(test_db) -> None:
    steps = _plan(
        test_db, FakeJelu(), _title(), _title(content_id="c2", title="Notes", isbn13=None)
    ).steps
    assert [s.action for s in steps] == [push.CREATE, push.LEFT_OUT]


def test_a_title_asked_for_is_pushed_without_an_isbn(test_db) -> None:
    (step,) = _plan(test_db, FakeJelu(), _title(isbn13=None), also={"c1"}).steps
    assert step.action == push.CREATE


def test_a_book_in_jelu_outside_the_library_is_used_not_duplicated(test_db) -> None:
    jelu = FakeJelu()
    book_id = jelu.add("Neural Networks", ["Brian Murray"], "9798224814305", in_library=False)
    todo = _plan(test_db, jelu, _title())
    (step,) = todo.steps
    assert (step.action, step.jelu.book_id) == (push.START, book_id)
    with get_connection(test_db) as conn:
        push.apply(conn, jelu, todo)
    assert jelu.created == []
    assert jelu.userbooks_by_book[book_id]["lastReadingEvent"] == push.READING


def test_a_weak_match_is_listed_and_not_acted_on(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["Someone Else"])
    todo = _plan(test_db, jelu, _title(isbn13=None))
    assert [s.action for s in todo.steps] == [push.WEAK]
    with get_connection(test_db) as conn:
        push.apply(conn, jelu, todo)
    assert jelu.events == []


# --- applying ---


def test_apply_sends_dated_events_and_a_rerun_changes_nothing(test_db) -> None:
    jelu = FakeJelu()
    reading = jelu.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305")
    titles = [
        _title(),
        _title(
            content_id="c2",
            title="Build a Large Language Model",
            isbn13="9781633437166",
            authors=["Sebastian Raschka"],
            status="finished",
        ),
    ]
    with get_connection(test_db) as conn:
        created, events, _, errors = push.apply(conn, jelu, _plan(test_db, jelu, *titles))
    assert (created, events, errors) == (1, 2, [])
    assert jelu.events[0] == (reading, push.READING, "2024-10-04T02:07:29Z")
    assert jelu.events[1][1] == push.FINISHED
    assert jelu.created[0]["isbn13"] == "9781633437166"
    assert _plan(test_db, jelu, *titles).changes == []


def test_one_failed_book_doesnt_stop_the_rest(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305")
    real = jelu.add_book

    def fail(*a, **k):
        raise RuntimeError("Jelu said no")

    jelu.add_book = fail
    todo = _plan(
        test_db, jelu, _title(content_id="c2", title="New", isbn13="9781633437166"), _title()
    )
    with get_connection(test_db) as conn:
        created, events, _, errors = push.apply(conn, jelu, todo)
    jelu.add_book = real
    assert (created, events) == (0, 1)
    assert errors == ["New: Jelu said no"]


def test_two_kobo_titles_of_one_new_book_create_it_once(test_db) -> None:
    jelu = FakeJelu()
    early = _title(content_id="epub", last_read=datetime(2026, 1, 1, tzinfo=UTC))
    late = _title(content_id="pdf", status="finished", last_read=datetime(2026, 2, 1, tzinfo=UTC))
    todo = _plan(test_db, jelu, early, late)
    assert [(s.kobo.content_id, s.action) for s in todo.steps] == [
        ("epub", push.SAME_BOOK),
        ("pdf", push.CREATE),
    ]
    with get_connection(test_db) as conn:
        created, events, _, _ = push.apply(conn, jelu, todo)
    assert (created, events) == (1, 1)
    assert jelu.events[0][1] == push.FINISHED


def test_two_kobo_titles_of_one_jelu_book_send_one_event(test_db) -> None:
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305")
    todo = _plan(test_db, jelu, _title(content_id="a"), _title(content_id="b"))
    assert sorted(s.action for s in todo.steps) == [push.SAME_BOOK, push.START]
    with get_connection(test_db) as conn:
        push.apply(conn, jelu, todo)
    assert len(jelu.events) == 1


def test_documents_sharing_the_start_of_a_title_are_both_pushed(test_db) -> None:
    a = _title(content_id="a", title="Kernel Ontology - part 1", isbn13=None, authors=[])
    b = _title(content_id="b", title="Kernel Ontology - part 2", isbn13=None, authors=[])
    todo = _plan(test_db, FakeJelu(), a, b, also={"a", "b"})
    assert [s.action for s in todo.steps] == [push.CREATE, push.CREATE]
    # Once part 1 is in Jelu, part 2 still isn't taken for it
    jelu = FakeJelu()
    jelu.add("Kernel Ontology - part 1", [])
    steps = _plan(test_db, jelu, a, b, also={"a", "b"}).steps
    assert [(s.kobo.content_id, s.action) for s in steps] == [("a", push.START), ("b", push.CREATE)]


# --- links in LeStash ---


def _kobo_and_jelu_items(test_db, device) -> tuple[int, int]:
    _store({}, test_db).take(device, datetime(2026, 11, 14, 9, 0, tzinfo=UTC))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))
        conn.execute(
            "INSERT INTO items (source_type, source_id, title, content, metadata)"
            " VALUES ('jelu', 'jelu:book:b1', 'Neural Networks for Beginners', '', '{}')"
        )
        conn.commit()
        (kobo_id,) = conn.execute(
            "SELECT id FROM items WHERE source_type = 'kobo' AND source_id = ?", (NN,)
        ).fetchone()
        (jelu_id,) = conn.execute(
            "SELECT id FROM items WHERE source_id = 'jelu:book:b1'"
        ).fetchone()
    return kobo_id, jelu_id


def test_a_match_is_linked_once_both_items_exist(test_db, device) -> None:
    kobo_id, jelu_id = _kobo_and_jelu_items(test_db, device)
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305")
    with get_connection(test_db) as conn:
        assert push.apply(conn, jelu, _plan(test_db, jelu, _title(content_id=NN)))[2] == 1
        row = conn.execute(
            "SELECT kind, evidence, origin, status FROM item_links WHERE from_id = ? AND to_id = ?",
            (kobo_id, jelu_id),
        ).fetchone()
        assert push.apply(conn, jelu, _plan(test_db, jelu, _title(content_id=NN)))[2] == 0
    assert tuple(row) == (
        "alternate_of",
        json.dumps({"isbn": "9798224814305"}),
        push.LINK_ORIGIN,
        "confirmed",
    )


def test_a_rejected_link_means_not_the_same_book(test_db, device) -> None:
    kobo_id, jelu_id = _kobo_and_jelu_items(test_db, device)
    with get_connection(test_db) as conn:
        conn.execute(
            "INSERT INTO item_links (from_id, to_id, kind, origin, status)"
            " VALUES (?, ?, 'alternate_of', 'manual', 'rejected')",
            (jelu_id, kobo_id),
        )
        conn.commit()
    jelu = FakeJelu()
    jelu.add("Neural Networks for Beginners", ["Brian Murray"], "9798224814305")
    (step,) = _plan(test_db, jelu, _title(content_id=NN)).steps
    assert step.action == push.REJECTED


# --- the command ---


def _cli(*args: str):
    result = CliRunner().invoke(KoboSource().get_commands(), list(args))
    return SimpleNamespace(exit_code=result.exit_code, output=" ".join(result.output.split()))


@pytest.fixture
def fake_jelu(monkeypatch) -> FakeJelu:
    import lestash_jelu.client as client

    jelu = FakeJelu()
    monkeypatch.setattr(client, "JeluClient", lambda *a, **k: jelu)
    monkeypatch.setattr(
        client,
        "load_credentials",
        lambda: {"url": jelu.url, "username": "mt", "password": "pw", "token": "t"},
    )
    return jelu


def test_the_command_shows_changes_and_sends_nothing_without_apply(
    test_db, device, fake_jelu
) -> None:
    _store({}, test_db).take(device, datetime(2026, 11, 14, 9, 0, tzinfo=UTC))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))
    result = _cli("push-jelu")
    assert result.exit_code == 0, result.output
    assert (
        "create in Jelu, then mark reading, 2024-10-04: Neural Networks for Beginners"
        in result.output
    )
    assert "Left out, not books (no ISBN, not in Jelu): 1" in result.output
    assert "Judge" in result.output
    assert "Nothing was sent; run with --apply" in result.output
    assert fake_jelu.events == []


def test_the_command_applies_logs_and_pushes_a_title_asked_for(test_db, device, fake_jelu) -> None:
    _store({}, test_db).take(device, datetime(2026, 11, 14, 9, 0, tzinfo=UTC))
    with get_connection(test_db) as conn:
        SyncStore(conn, "kobo").run(KoboSource().sync({}))
        (judge,) = conn.execute("SELECT id FROM items WHERE source_id = ?", (DRIVE_PDF,)).fetchone()
    result = _cli("push-jelu", "--apply", "--title", str(judge))
    assert result.exit_code == 0, result.output
    assert "Done: 2 book(s) created, 2 status(es) sent" in result.output
    with get_connection(test_db) as conn:
        row = conn.execute(
            "SELECT status, items_added, items_updated FROM sync_log WHERE source_type = ?",
            (push.LOG_SOURCE,),
        ).fetchone()
    assert tuple(row) == ("completed", 2, 2)
    assert "Nothing to send" in _cli("push-jelu").output


def test_a_title_that_isnt_a_kobo_book_is_refused(test_db, device, fake_jelu) -> None:
    _store({}, test_db).take(device, datetime(2026, 11, 14, 9, 0, tzinfo=UTC))
    result = _cli("push-jelu", "--title", "999999")
    assert result.exit_code == 1
    assert "isn't a Kobo book" in result.output


# --- a real Jelu, in a test environment ---


@pytest.mark.skipif(not os.environ.get("JELU_TEST_URL"), reason="needs a throwaway Jelu")
def test_against_a_throwaway_jelu(test_db) -> None:
    """Run with JELU_TEST_URL, JELU_TEST_USER and JELU_TEST_PASSWORD pointing at a
    throwaway Jelu (never the live one)."""
    from lestash_jelu.client import JeluClient

    with JeluClient(
        os.environ["JELU_TEST_URL"], os.environ["JELU_TEST_USER"], os.environ["JELU_TEST_PASSWORD"]
    ) as client:
        titles = [
            _title(
                content_id="r1",
                title="Push Test Reading",
                isbn13="9781633437166",
                authors=["Ann Lee"],
            ),
            _title(
                content_id="f1",
                title="Push Test Finished",
                isbn13="9780306406157",
                status="finished",
                authors=["Bo Chen"],
            ),
        ]
        books = push.jelu_books(client.userbooks(), client.books())
        with get_connection(test_db) as conn:
            todo = push.plan(conn, titles, books)
            created, events, _, errors = push.apply(conn, client, todo)
        assert errors == []
        after = push.jelu_books(client.userbooks(), client.books())
        status = {b.title: b.last_event for b in after if b.title.startswith("Push Test")}
        assert status == {
            "Push Test Reading": "CURRENTLY_READING",
            "Push Test Finished": "FINISHED",
        }
        with get_connection(test_db) as conn:
            assert push.plan(conn, titles, after).changes == []
        # Kobo finishes the book being read: Jelu closes the open reading event
        titles[0].status = "finished"
        with get_connection(test_db) as conn:
            push.apply(conn, client, push.plan(conn, titles, after))
        final = push.jelu_books(client.userbooks(), client.books())
        reading = next(b for b in final if b.title == "Push Test Reading")
        assert reading.last_event == "FINISHED"
        assert len([b for b in final if b.title.startswith("Push Test")]) == 2
        # Finishing closed the open reading event rather than adding a second one
        events = client._get("/reading-events/me", {"size": 50})["content"]
        assert sorted(e["userBook"]["book"]["title"] for e in events) == [
            "Push Test Finished",
            "Push Test Reading",
        ]

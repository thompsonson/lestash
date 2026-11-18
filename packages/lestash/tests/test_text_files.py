"""Text and Markdown files added as items."""

import pytest
from lestash.core.database import get_connection
from lestash.core.text_files import add_text_file, text_format


@pytest.fixture
def test_conn(test_db):
    with get_connection(test_db) as conn:
        yield conn


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("a.txt", "text"),
        ("A.MD", "markdown"),
        ("b.markdown", "markdown"),
        ("c.pdf", None),
        ("md", None),
    ],
)
def test_text_format(name, fmt):
    assert text_format(name) == fmt


def test_the_same_bytes_are_added_once(test_conn):
    one = add_text_file(test_conn, b"hello", "a.txt")
    two = add_text_file(test_conn, b"hello", "renamed.md")
    assert one.new and not two.new
    assert two.item_id == one.item_id


def test_an_empty_file_has_a_placeholder(test_conn):
    added = add_text_file(test_conn, b"", "empty.txt")
    content = test_conn.execute(
        "SELECT content FROM items WHERE id = ?", (added.item_id,)
    ).fetchone()[0]
    assert content == "[Empty file: empty.txt]"


def test_another_file_type_is_refused(test_conn):
    with pytest.raises(ValueError):
        add_text_file(test_conn, b"x", "a.pdf")

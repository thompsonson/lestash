"""Test fixtures for Audible plugin."""

import pytest


@pytest.fixture
def sample_book() -> dict:
    """Sample Audible library book entry."""
    return {
        "asin": "B08G9PRS1K",
        "title": "Project Hail Mary",
        "subtitle": "A Novel",
        "authors": [{"name": "Andy Weir"}],
        "narrators": [{"name": "Ray Porter"}],
        "runtime_length_min": 978,
        "release_date": "2021-05-04",
        "publisher_name": "Audible Studios",
        "language": "english",
        "product_images": {"500": "https://m.media-amazon.com/images/I/cover.jpg"},
        "series": [{"title": "Project Hail Mary", "sequence": "1"}],
        "format_type": "unabridged",
        "content_type": "Product",
    }


@pytest.fixture
def sample_book_no_extras() -> dict:
    """Minimal book entry with no series, narrators, or subtitle."""
    return {
        "asin": "B000FA5ZEG",
        "title": "The Hitchhiker's Guide to the Galaxy",
        "authors": [{"name": "Douglas Adams"}],
        "narrators": [],
        "runtime_length_min": 342,
        "release_date": "2005-09-01",
        "publisher_name": "Pan Books",
        "language": "english",
        "product_images": {},
        "series": [],
    }


@pytest.fixture
def sample_bookmark_with_note() -> dict:
    """Bookmark with a user note."""
    return {
        "type": "note",
        "position": 3_600_000,
        "note": "This is the key insight about the Astrophage.",
        "creationTime": "2026-01-15T10:30:00Z",
    }


@pytest.fixture
def sample_bookmark_no_note() -> dict:
    """Bookmark without a note (position-only)."""
    return {
        "type": "bookmark",
        "position": 7_200_000,
        "note": "",
        "creationTime": "2026-01-16T14:00:00Z",
    }


@pytest.fixture
def sample_sidecar_response(sample_bookmark_with_note, sample_bookmark_no_note) -> dict:
    """Sample sidecar endpoint response."""
    return {
        "bookmarks": [sample_bookmark_no_note],
        "notes": [sample_bookmark_with_note],
    }

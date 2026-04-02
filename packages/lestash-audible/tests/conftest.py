"""Test fixtures for Audible plugin.

Fixtures match the real Audible sidecar API response format.
"""

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
def sample_note() -> dict:
    """Note record from sidecar API (audible.note type)."""
    return {
        "type": "audible.note",
        "annotationId": "a1A3VFUSQG5UQ0",
        "startPosition": "3600000",
        "endPosition": "3600000",
        "creationTime": "2026-01-15 10:30:00.0",
        "text": "This is the key insight about the Astrophage.",
        "lastModificationTime": "2026-01-15 10:30:00.0",
    }


@pytest.fixture
def sample_clip() -> dict:
    """Clip record from sidecar API (audible.clip type with metadata.note)."""
    return {
        "type": "audible.clip",
        "annotationId": "a3LPJUGNSGN37A",
        "startPosition": "3600000",
        "endPosition": "3630000",
        "creationTime": "2026-01-15 10:30:00.0",
        "metadata": {
            "note": "This is the key insight about the Astrophage.",
            "c_version": "10291671",
        },
        "lastModificationTime": "2026-01-15 10:30:00.0",
    }


@pytest.fixture
def sample_bookmark() -> dict:
    """Bookmark record from sidecar API (position-only, no text)."""
    return {
        "type": "audible.bookmark",
        "annotationId": "a2ZRCY6982QR1F",
        "startPosition": "7200000",
        "creationTime": "2026-01-16 14:00:00.0",
        "lastModificationTime": "2026-01-16 14:00:00.0",
    }


@pytest.fixture
def sample_last_heard() -> dict:
    """System record for last listening position (should be filtered out)."""
    return {
        "type": "audible.last_heard",
        "startPosition": "5783528",
        "creationTime": "2026-03-17 22:19:05.0",
    }


@pytest.fixture
def sample_records(sample_note, sample_clip, sample_bookmark, sample_last_heard) -> list:
    """Realistic sidecar records list including duplicates and system records."""
    return [sample_last_heard, sample_note, sample_clip, sample_bookmark]

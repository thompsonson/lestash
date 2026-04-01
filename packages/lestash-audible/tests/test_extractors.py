"""Tests for Audible item extraction."""

from lestash.models.item import ItemCreate
from lestash_audible.client import extract_book_metadata
from lestash_audible.source import (
    _extract_annotations,
    book_to_item,
    bookmark_to_item,
    format_position,
)


class TestFormatPosition:
    """Test position formatting."""

    def test_seconds_only(self):
        assert format_position(45_000) == "0:45"

    def test_minutes_and_seconds(self):
        assert format_position(150_000) == "2:30"

    def test_hours(self):
        assert format_position(3_661_000) == "1:01:01"

    def test_zero(self):
        assert format_position(0) == "0:00"


class TestBookToItem:
    """Test book to ItemCreate conversion."""

    def test_creates_item(self, sample_book):
        item = book_to_item(sample_book)
        assert isinstance(item, ItemCreate)

    def test_source_type(self, sample_book):
        item = book_to_item(sample_book)
        assert item.source_type == "audible"

    def test_source_id(self, sample_book):
        item = book_to_item(sample_book)
        assert item.source_id == "audible:book:B08G9PRS1K"

    def test_title(self, sample_book):
        item = book_to_item(sample_book)
        assert item.title == "Project Hail Mary"

    def test_author(self, sample_book):
        item = book_to_item(sample_book)
        assert item.author == "Andy Weir"

    def test_url(self, sample_book):
        item = book_to_item(sample_book)
        assert item.url == "https://www.audible.com/pd/B08G9PRS1K"

    def test_content_includes_author(self, sample_book):
        item = book_to_item(sample_book)
        assert "Andy Weir" in item.content

    def test_content_includes_narrator(self, sample_book):
        item = book_to_item(sample_book)
        assert "Ray Porter" in item.content

    def test_content_includes_series(self, sample_book):
        item = book_to_item(sample_book)
        assert "Series:" in item.content

    def test_content_includes_runtime(self, sample_book):
        item = book_to_item(sample_book)
        assert "16h 18m" in item.content

    def test_metadata_has_asin(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["asin"] == "B08G9PRS1K"

    def test_metadata_type_is_book(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["type"] == "book"

    def test_not_own_content(self, sample_book):
        item = book_to_item(sample_book)
        assert item.is_own_content is False

    def test_minimal_book(self, sample_book_no_extras):
        item = book_to_item(sample_book_no_extras)
        assert item.title == "The Hitchhiker's Guide to the Galaxy"
        assert "Narrated by" not in item.content
        assert "Series:" not in item.content


class TestBookmarkToItem:
    """Test bookmark/note to ItemCreate conversion."""

    def test_note_creates_item(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert isinstance(item, ItemCreate)

    def test_note_content(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert "key insight about the Astrophage" in item.content

    def test_note_title_includes_book(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert "Project Hail Mary" in item.title
        assert "1:00:00" in item.title

    def test_bookmark_without_note(self, sample_bookmark_no_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_no_note, meta)
        assert item.content == "Bookmark at 2:00:00"

    def test_source_id_format(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert item.source_id == "audible:bookmark:B08G9PRS1K:3600000"

    def test_is_own_content(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert item.is_own_content is True

    def test_has_parent_marker(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert item.metadata["_parent_source_id"] == "audible:book:B08G9PRS1K"

    def test_created_at_parsed(self, sample_bookmark_with_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark_with_note, meta)
        assert item.created_at is not None
        assert item.created_at.year == 2026


class TestExtractAnnotations:
    """Test sidecar response parsing."""

    def test_extracts_bookmarks_and_notes(self, sample_sidecar_response):
        annotations = _extract_annotations(sample_sidecar_response)
        assert len(annotations) == 2

    def test_empty_response(self):
        assert _extract_annotations({}) == []

    def test_handles_clips_key(self):
        sidecar = {"clips": [{"position": 1000, "note": "clip text"}]}
        annotations = _extract_annotations(sidecar)
        assert len(annotations) == 1

    def test_ignores_non_list_values(self):
        sidecar = {"bookmarks": "not a list"}
        annotations = _extract_annotations(sidecar)
        assert len(annotations) == 0

    def test_ignores_non_dict_items(self):
        sidecar = {"bookmarks": ["not a dict", 42]}
        annotations = _extract_annotations(sidecar)
        assert len(annotations) == 0


class TestExtractBookMetadata:
    """Test book metadata extraction."""

    def test_extracts_asin(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert meta["asin"] == "B08G9PRS1K"

    def test_extracts_authors(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert meta["authors"] == ["Andy Weir"]

    def test_extracts_narrators(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert meta["narrators"] == ["Ray Porter"]

    def test_extracts_series(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert len(meta["series"]) == 1
        assert meta["series"][0]["name"] == "Project Hail Mary"

    def test_handles_missing_fields(self):
        meta = extract_book_metadata({"asin": "X123"})
        assert meta["asin"] == "X123"
        assert meta["authors"] == []
        assert meta["narrators"] == []
        assert meta["series"] == []

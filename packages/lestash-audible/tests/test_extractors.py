"""Tests for Audible item extraction.

Tests use fixtures matching the real Audible sidecar API response format.
"""

from lestash.models.item import ItemCreate
from lestash_audible.client import extract_book_metadata
from lestash_audible.source import (
    _deduplicate_annotations,
    _extract_annotations,
    _find_chapter,
    _get_note_text,
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

    def test_content_is_description(self, sample_book):
        item = book_to_item(sample_book)
        assert "lone astronaut must save the earth" in item.content

    def test_content_strips_html(self, sample_book):
        item = book_to_item(sample_book)
        assert "<p>" not in item.content

    def test_content_fallback_without_description(self, sample_book_no_extras):
        item = book_to_item(sample_book_no_extras)
        assert "Hitchhiker" in item.content
        assert "Douglas Adams" in item.content

    def test_metadata_has_asin(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["asin"] == "B08G9PRS1K"

    def test_metadata_type_is_book(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["type"] == "book"

    def test_metadata_has_rating(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["rating"] == 4.8
        assert item.metadata["rating_count"] == 50000

    def test_metadata_has_categories(self, sample_book):
        item = book_to_item(sample_book)
        assert "Science Fiction" in item.metadata["categories"]

    def test_metadata_has_progress(self, sample_book):
        item = book_to_item(sample_book)
        assert item.metadata["percent_complete"] == 72.5
        assert item.metadata["is_finished"] is False

    def test_metadata_has_cover_urls(self, sample_book):
        item = book_to_item(sample_book)
        assert "500" in item.metadata["cover_urls"]

    def test_media_has_cover_thumbnail(self, sample_book):
        item = book_to_item(sample_book)
        assert item.media is not None
        assert len(item.media) == 1
        assert item.media[0].media_type == "thumbnail"
        assert "cover.jpg" in item.media[0].url

    def test_chapters_stored_in_metadata(self, sample_book, sample_chapters):
        item = book_to_item(sample_book, chapters=sample_chapters)
        assert len(item.metadata["chapters"]) == 3

    def test_not_own_content(self, sample_book):
        item = book_to_item(sample_book)
        assert item.is_own_content is False

    def test_minimal_book(self, sample_book_no_extras):
        item = book_to_item(sample_book_no_extras)
        assert item.title == "The Hitchhiker's Guide to the Galaxy"


class TestGetNoteText:
    """Test note text extraction from different record types."""

    def test_extracts_from_text_field(self, sample_note):
        assert "key insight about the Astrophage" in _get_note_text(sample_note)

    def test_extracts_from_metadata_note(self, sample_clip):
        assert "key insight about the Astrophage" in _get_note_text(sample_clip)

    def test_returns_empty_for_bookmark(self, sample_bookmark):
        assert _get_note_text(sample_bookmark) == ""

    def test_returns_empty_for_empty_record(self):
        assert _get_note_text({}) == ""


class TestBookmarkToItem:
    """Test bookmark/note to ItemCreate conversion."""

    def test_note_creates_item(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert isinstance(item, ItemCreate)

    def test_note_content(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert "key insight about the Astrophage" in item.content

    def test_note_title_includes_book(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert "Project Hail Mary" in item.title
        assert "1:00:00" in item.title

    def test_clip_extracts_note_text(self, sample_clip, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_clip, meta)
        assert "key insight about the Astrophage" in item.content

    def test_bookmark_without_note(self, sample_bookmark, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_bookmark, meta)
        assert item.content == "Bookmark at 2:00:00"

    def test_source_id_uses_annotation_id(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert item.source_id == "audible:annotation:B08G9PRS1K:a1A3VFUSQG5UQ0"

    def test_is_own_content(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert item.is_own_content is True

    def test_has_parent_marker(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert item.metadata["_parent_source_id"] == "audible:book:B08G9PRS1K"

    def test_created_at_parsed(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert item.created_at is not None
        assert item.created_at.year == 2026
        assert item.created_at.month == 1
        assert item.created_at.day == 15

    def test_position_in_metadata(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert item.metadata["position_ms"] == 3600000
        assert item.metadata["position_str"] == "1:00:00"

    def test_chapter_in_title(self, sample_note, sample_book, sample_chapters):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta, chapters=sample_chapters)
        assert "Chapter 1: The Problem" in item.title
        assert "Project Hail Mary" in item.title

    def test_chapter_in_metadata(self, sample_note, sample_book, sample_chapters):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta, chapters=sample_chapters)
        assert item.metadata["chapter"] == "Chapter 1: The Problem"

    def test_no_chapter_without_chapters_param(self, sample_note, sample_book):
        meta = extract_book_metadata(sample_book)
        item = bookmark_to_item(sample_note, meta)
        assert "chapter" not in item.metadata


class TestFindChapter:
    """Test chapter resolution from position."""

    def test_finds_matching_chapter(self, sample_chapters):
        assert _find_chapter(3600000, sample_chapters) == "Chapter 1: The Problem"

    def test_finds_first_chapter(self, sample_chapters):
        assert _find_chapter(0, sample_chapters) == "Opening Credits"

    def test_finds_last_chapter(self, sample_chapters):
        assert _find_chapter(5000000, sample_chapters) == "Chapter 2: The Solution"

    def test_returns_none_past_end(self, sample_chapters):
        assert _find_chapter(99999999, sample_chapters) is None

    def test_returns_none_empty_chapters(self):
        assert _find_chapter(1000, []) is None


class TestDeduplicateAnnotations:
    """Test deduplication of records at the same position."""

    def test_keeps_note_over_clip_and_bookmark(self):
        records = [
            {"type": "audible.bookmark", "startPosition": "1000", "annotationId": "a1"},
            {"type": "audible.clip", "startPosition": "1000", "annotationId": "a2"},
            {"type": "audible.note", "startPosition": "1000", "annotationId": "a3", "text": "hi"},
        ]
        result = _deduplicate_annotations(records)
        assert len(result) == 1
        assert result[0]["type"] == "audible.note"

    def test_keeps_clip_over_bookmark(self):
        records = [
            {"type": "audible.bookmark", "startPosition": "1000", "annotationId": "a1"},
            {"type": "audible.clip", "startPosition": "1000", "annotationId": "a2"},
        ]
        result = _deduplicate_annotations(records)
        assert len(result) == 1
        assert result[0]["type"] == "audible.clip"

    def test_keeps_different_positions(self):
        records = [
            {"type": "audible.bookmark", "startPosition": "1000", "annotationId": "a1"},
            {"type": "audible.bookmark", "startPosition": "2000", "annotationId": "a2"},
        ]
        result = _deduplicate_annotations(records)
        assert len(result) == 2

    def test_empty_list(self):
        assert _deduplicate_annotations([]) == []

    def test_deduplicates_int_and_str_positions(self):
        """Regression: API may return startPosition as int or str."""
        records = [
            {"type": "audible.bookmark", "startPosition": 0, "annotationId": "a1"},
            {"type": "audible.clip", "startPosition": "0", "annotationId": "a2"},
            {"type": "audible.note", "startPosition": 0, "annotationId": "a3", "text": "hi"},
        ]
        result = _deduplicate_annotations(records)
        assert len(result) == 1
        assert result[0]["type"] == "audible.note"

    def test_deduplicates_missing_start_position(self):
        """Records without startPosition should dedup to position 0."""
        records = [
            {"type": "audible.bookmark", "annotationId": "a1"},
            {"type": "audible.note", "startPosition": 0, "annotationId": "a2", "text": "hi"},
        ]
        result = _deduplicate_annotations(records)
        assert len(result) == 1
        assert result[0]["type"] == "audible.note"


class TestExtractAnnotations:
    """Test filtering and deduplication pipeline."""

    def test_filters_last_heard_and_deduplicates(self, sample_records):
        # sample_records has: last_heard + note + clip (same pos) + bookmark (diff pos)
        annotations = _extract_annotations(sample_records)
        # last_heard filtered, note+clip deduped to note, bookmark kept = 2
        assert len(annotations) == 2

    def test_empty_list(self):
        assert _extract_annotations([]) == []

    def test_filters_last_heard(self):
        records = [
            {"type": "audible.last_heard", "startPosition": "1000"},
            {"type": "audible.note", "startPosition": "2000", "text": "keep"},
        ]
        annotations = _extract_annotations(records)
        assert len(annotations) == 1
        assert annotations[0]["type"] == "audible.note"

    def test_ignores_non_dict_items(self):
        records = ["not a dict", 42, {"type": "audible.bookmark", "startPosition": "1000"}]
        annotations = _extract_annotations(records)
        assert len(annotations) == 1


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

    def test_handles_null_series(self):
        """API returns null instead of missing key for some fields."""
        meta = extract_book_metadata({"asin": "X", "series": None, "authors": None})
        assert meta["series"] == []
        assert meta["authors"] == []

    def test_extracts_description(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert "lone astronaut must save the earth" in meta["description"]

    def test_strips_html_from_description(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert "<p>" not in meta["description"]

    def test_extracts_rating(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert meta["rating"] == 4.8
        assert meta["rating_count"] == 50000

    def test_extracts_categories(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert "Science Fiction & Fantasy" in meta["categories"]
        assert "Science Fiction" in meta["categories"]

    def test_extracts_progress(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert meta["percent_complete"] == 72.5
        assert meta["is_finished"] is False

    def test_extracts_cover_urls(self, sample_book):
        meta = extract_book_metadata(sample_book)
        assert "500" in meta["cover_urls"]

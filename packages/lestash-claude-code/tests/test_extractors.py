"""Tests for title cleanup, content building, and session-to-item conversion."""

from lestash.models.item import ItemCreate
from lestash_claude_code.source import build_content, clean_title, session_to_item


class TestCleanTitle:
    """Test the 5-rule title cleanup."""

    def test_normal_title_passes_through(self):
        result = clean_title("review the auth middleware", "lestash", "main", "2026-04-05T09:00:00")
        assert result == "review the auth middleware"

    def test_xml_prefix_uses_fallback(self):
        result = clean_title(
            "<local-command-caveat>Caveat: messages...",
            "lestash",
            "main",
            "2026-04-05T09:00:00",
        )
        assert result == "lestash — main (2026-04-05)"

    def test_none_title_uses_fallback(self):
        result = clean_title(None, "myproject", "feat/auth", "2026-04-05T09:00:00")
        assert result == "myproject — feat/auth (2026-04-05)"

    def test_empty_title_uses_fallback(self):
        result = clean_title("", "proj", "dev", "2026-04-05T09:00:00")
        assert result == "proj — dev (2026-04-05)"

    def test_short_title_uses_fallback(self):
        result = clean_title("hi", "proj", "main", "2026-04-05T09:00:00")
        assert result == "proj — main (2026-04-05)"

    def test_exactly_5_chars_passes(self):
        result = clean_title("hello", "proj", "main", "2026-04-05T09:00:00")
        assert result == "hello"

    def test_long_title_truncated(self):
        long_title = "a" * 150
        result = clean_title(long_title, "proj", "main", "2026-04-05T09:00:00")
        assert len(result) == 120
        assert result.endswith("…")
        assert result == "a" * 119 + "…"

    def test_exactly_120_chars_not_truncated(self):
        title = "a" * 120
        result = clean_title(title, "proj", "main", "2026-04-05T09:00:00")
        assert result == title

    def test_newline_in_title_truncates(self):
        result = clean_title("first line\nsecond line", "proj", "main", "2026-04-05T09:00:00")
        assert result == "first line"

    def test_whitespace_stripped(self):
        result = clean_title("  hello world  ", "proj", "main", "2026-04-05T09:00:00")
        assert result == "hello world"

    def test_no_branch_fallback(self):
        result = clean_title(None, "myproject", None, "2026-04-05T09:00:00")
        assert result == "myproject (2026-04-05)"

    def test_fallback_with_branch(self):
        result = clean_title(None, "lestash", "feat/import", "2026-04-05T09:00:00")
        assert result == "lestash — feat/import (2026-04-05)"


class TestBuildContent:
    """Test the content template builder."""

    def test_full_content_with_message(self):
        content = build_content(
            first_message="review the auth middleware for security issues",
            duration="8m 30s",
            project="lestash",
            branch="feat/auth",
            files_touched=["src/auth.py", "tests/test_auth.py"],
            files_touched_count=2,
            tools_used={"Bash": 17, "Grep": 8},
            message_count=109,
            user_messages=47,
            assistant_messages=51,
        )
        lines = content.split("\n")
        assert lines[0] == "review the auth middleware for security issues"
        assert lines[1] == ""
        assert "8m 30s session on lestash (feat/auth branch)." in lines[2]
        assert "Touched 2 files: auth.py, test_auth.py." in lines[3]
        assert "Tools: Bash (17), Grep (8)." in lines[4]
        assert "109 messages (47 user, 51 assistant)." in lines[5]

    def test_content_without_message(self):
        content = build_content(
            first_message=None,
            duration="2m",
            project="proj",
            branch="main",
            files_touched=[],
            files_touched_count=0,
            tools_used={"Read": 1},
            message_count=10,
            user_messages=5,
            assistant_messages=5,
        )
        lines = content.split("\n")
        # No empty line prefix when no message
        assert lines[0] == "2m session on proj (main branch)."
        assert "Tools: Read (1)." in lines[1]

    def test_content_no_branch(self):
        content = build_content(
            first_message=None,
            duration="5m",
            project="proj",
            branch=None,
            files_touched=[],
            files_touched_count=0,
            tools_used={},
            message_count=4,
            user_messages=2,
            assistant_messages=2,
        )
        assert "5m session on proj." in content
        assert "branch" not in content

    def test_content_no_tools(self):
        content = build_content(
            first_message=None,
            duration="1m",
            project="proj",
            branch="main",
            files_touched=[],
            files_touched_count=0,
            tools_used={},
            message_count=2,
            user_messages=1,
            assistant_messages=1,
        )
        assert "Tools:" not in content

    def test_content_files_touched_count_only(self):
        content = build_content(
            first_message=None,
            duration="3m",
            project="proj",
            branch="main",
            files_touched=[],
            files_touched_count=5,
            tools_used={},
            message_count=10,
            user_messages=5,
            assistant_messages=5,
        )
        assert "Touched 5 files." in content

    def test_files_show_basenames(self):
        content = build_content(
            first_message=None,
            duration="3m",
            project="proj",
            branch="main",
            files_touched=["/home/user/project/src/deep/nested/file.py"],
            files_touched_count=1,
            tools_used={},
            message_count=2,
            user_messages=1,
            assistant_messages=1,
        )
        assert "file.py" in content
        assert "/home/user" not in content


class TestSessionToItem:
    """Test session-to-ItemCreate conversion."""

    def test_basic_conversion(self, session_summary_factory, session_detail_factory):
        summary = session_summary_factory()
        detail = session_detail_factory()
        first_msg = "review the auth middleware"

        item = session_to_item(summary, detail, first_msg)

        assert isinstance(item, ItemCreate)
        assert item.source_type == "claude-code"
        assert item.source_id == summary.session
        assert item.author == "user"
        assert item.is_own_content is True
        assert item.url is None
        assert item.parent_id is None

    def test_metadata_fields(self, session_summary_factory, session_detail_factory):
        summary = session_summary_factory(project="lestash", size_kb=235)
        detail = session_detail_factory(
            git_branch="feat/auth",
            duration="8m",
            cwd="/home/user/Projects/lestash",
            version="2.1.87",
        )

        item = session_to_item(summary, detail, None)

        assert item.metadata["project"] == "lestash"
        assert item.metadata["branch"] == "feat/auth"
        assert item.metadata["duration"] == "8m"
        assert item.metadata["size_kb"] == 235
        assert item.metadata["cwd"] == "/home/user/Projects/lestash"
        assert item.metadata["claude_code_version"] == "2.1.87"
        assert item.metadata["hostname"]  # should be set from socket.gethostname()
        assert item.metadata["message_count"] == 57
        assert item.metadata["tools_used"] == {"Bash": 6, "Agent": 3}

    def test_created_at_from_modified(self, session_summary_factory, session_detail_factory):
        summary = session_summary_factory(modified="2026-04-05T09:03:36.546401")
        detail = session_detail_factory()

        item = session_to_item(summary, detail, None)

        assert item.created_at is not None
        assert item.created_at.year == 2026
        assert item.created_at.month == 4
        assert item.created_at.day == 5

    def test_title_cleaned(self, session_summary_factory, session_detail_factory):
        summary = session_summary_factory(project="lestash")
        detail = session_detail_factory(title="<local-command-caveat>Caveat...", git_branch="main")

        item = session_to_item(summary, detail, None)

        assert item.title == "lestash — main (2026-04-05)"

    def test_content_includes_first_message(self, session_summary_factory, session_detail_factory):
        summary = session_summary_factory()
        detail = session_detail_factory()

        item = session_to_item(summary, detail, "fix the login bug")

        assert "fix the login bug" in item.content

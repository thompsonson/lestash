"""Tests for Reveal subprocess wrapper and JSONL parser."""

from unittest.mock import patch

import pytest
from lestash_claude_code.client import (
    check_reveal,
    extract_first_substantive_message,
    get_session_detail,
    list_sessions,
)


class TestCheckReveal:
    """Test reveal binary detection."""

    def test_returns_true_when_installed(self):
        with patch("lestash_claude_code.client.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert check_reveal() is True

    def test_returns_false_when_missing(self):
        with patch("lestash_claude_code.client.subprocess.run", side_effect=FileNotFoundError):
            assert check_reveal() is False


class TestListSessions:
    """Test session enumeration via Reveal."""

    def test_lists_all_sessions(self, reveal_sessions_response):
        with patch("lestash_claude_code.client._run_reveal") as mock:
            mock.return_value = reveal_sessions_response

            sessions = list_sessions()

            mock.assert_called_once_with(["claude://sessions/", "--all"], timeout=60)
            assert len(sessions) == 3
            assert sessions[0].session == "aaaa1111-0000-0000-0000-000000000001"
            assert sessions[0].project == "lestash"
            assert sessions[0].size_kb == 156

    def test_lists_with_since_filter(self, reveal_sessions_response):
        with patch("lestash_claude_code.client._run_reveal") as mock:
            mock.return_value = reveal_sessions_response

            list_sessions(since="2026-04-01")

            mock.assert_called_once_with(
                ["claude://sessions/", "--since", "2026-04-01"], timeout=60
            )

    def test_handles_missing_optional_fields(self):
        response = {
            "recent_sessions": [
                {
                    "session": "test-uuid",
                    "path": "/tmp/test.jsonl",
                    "modified": "2026-04-05T00:00:00",
                }
            ]
        }
        with patch("lestash_claude_code.client._run_reveal", return_value=response):
            sessions = list_sessions()

            assert len(sessions) == 1
            assert sessions[0].size_kb == 0
            assert sessions[0].project == "unknown"
            assert sessions[0].title is None

    def test_raises_on_reveal_failure(self):
        with (
            patch(
                "lestash_claude_code.client._run_reveal",
                side_effect=RuntimeError("reveal failed"),
            ),
            pytest.raises(RuntimeError, match="reveal failed"),
        ):
            list_sessions()


class TestGetSessionDetail:
    """Test session overview fetching."""

    def test_parses_detail_response(
        self,
        reveal_session_detail_response,
        reveal_tools_response,
        reveal_files_response,
    ):
        sid = "aaaa1111-0000-0000-0000-000000000001"

        def mock_reveal(args, timeout=30):
            uri = args[0]
            if uri.endswith("/tools"):
                return reveal_tools_response
            if uri.endswith("/files"):
                return reveal_files_response
            return reveal_session_detail_response

        with patch("lestash_claude_code.client._run_reveal", side_effect=mock_reveal):
            detail = get_session_detail(sid)

            assert detail.message_count == 109
            assert detail.user_messages == 47
            assert detail.assistant_messages == 51
            assert detail.tools_used == {"Bash": 17, "Grep": 8, "WebFetch": 6, "Read": 2}
            assert detail.files_touched == ["src/auth.py", "tests/test_auth.py"]
            assert detail.duration == "8m 30s"
            assert detail.cwd == "/home/user/Projects/lestash"
            assert detail.git_branch == "feat/auth"
            assert detail.version == "2.1.87"

            # Tool details from /tools subview
            assert detail.tool_details["Bash"]["success_rate"] == "85.2%"
            assert detail.tool_details["Bash"]["failure"] == 3
            assert detail.tool_details["Read"]["success_rate"] == "100.0%"

            # File operations from /files subview
            assert detail.file_operations["Read"]["src/auth.py"] == 3
            assert detail.file_operations["Edit"]["tests/test_auth.py"] == 1

    def test_handles_empty_context(self):
        response = {
            "session": "test-uuid",
            "message_count": 5,
        }
        with patch("lestash_claude_code.client._run_reveal", return_value=response):
            detail = get_session_detail("test-uuid")

            assert detail.cwd is None
            assert detail.git_branch is None
            assert detail.version is None
            assert detail.tools_used == {}
            assert detail.files_touched == []
            # Subview calls return same empty response, parsed gracefully
            assert detail.tool_details == {}
            assert detail.file_operations == {}

    def test_subview_failures_dont_break_detail(self, reveal_session_detail_response):
        """Tool/file subviews failing should not prevent detail from returning."""

        def mock_reveal(args, timeout=30):
            uri = args[0]
            if "/tools" in uri or "/files" in uri:
                raise RuntimeError("subview failed")
            return reveal_session_detail_response

        with patch("lestash_claude_code.client._run_reveal", side_effect=mock_reveal):
            detail = get_session_detail("aaaa1111-0000-0000-0000-000000000001")

            assert detail.message_count == 109
            assert detail.tool_details == {}
            assert detail.file_operations == {}


class TestExtractFirstSubstantiveMessage:
    """Test JSONL parsing for first substantive user message."""

    def test_skips_xml_and_short_messages(self, sample_jsonl_file):
        path = sample_jsonl_file()
        result = extract_first_substantive_message(path)
        assert result == "review the auth middleware for security issues"

    def test_skips_single_word_messages(self, sample_jsonl_file):
        path = sample_jsonl_file(
            [
                ("user", "continue"),
                ("user", "this is the actual question I want to ask"),
            ]
        )
        result = extract_first_substantive_message(path)
        assert result == "this is the actual question I want to ask"

    def test_skips_messages_under_20_chars(self, sample_jsonl_file):
        path = sample_jsonl_file(
            [
                ("user", "fix this bug"),
                ("user", "the authentication module needs a complete rewrite"),
            ]
        )
        result = extract_first_substantive_message(path)
        assert result == "the authentication module needs a complete rewrite"

    def test_returns_none_when_no_qualifying_message(self, sample_jsonl_file):
        path = sample_jsonl_file(
            [
                ("user", "<command>test</command>"),
                ("user", "yes"),
                ("assistant", "this is a long assistant message that should not match"),
            ]
        )
        result = extract_first_substantive_message(path)
        assert result is None

    def test_truncates_at_200_chars(self, sample_jsonl_file):
        long_message = "this is a long message " * 20  # ~460 chars with spaces
        path = sample_jsonl_file([("user", long_message)])
        result = extract_first_substantive_message(path)
        assert result is not None
        assert len(result) == 200

    def test_handles_list_format_content(self, sample_jsonl_file):
        path = sample_jsonl_file(
            [
                (
                    "user",
                    [{"type": "text", "text": "review the middleware implementation please"}],
                ),
            ]
        )
        result = extract_first_substantive_message(path)
        assert result == "review the middleware implementation please"

    def test_returns_none_for_missing_file(self):
        result = extract_first_substantive_message("/nonexistent/path.jsonl")
        assert result is None

    def test_skips_malformed_json_lines(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text(
            'not valid json\n{"type": "user", "content": "this is a real user message here"}\n'
        )
        result = extract_first_substantive_message(str(path))
        assert result == "this is a real user message here"

    def test_skips_non_user_messages(self, sample_jsonl_file):
        path = sample_jsonl_file(
            [
                ("system", "this is a long system message that should not match"),
                ("assistant", "this is a long assistant message that should not match"),
                ("user", "finally a real user message with enough content"),
            ]
        )
        result = extract_first_substantive_message(path)
        assert result == "finally a real user message with enough content"

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        result = extract_first_substantive_message(str(path))
        assert result is None

"""Tests for Claude Code CLI commands."""

from unittest.mock import patch

import pytest
from lestash_claude_code.client import SessionDetail, SessionSummary
from lestash_claude_code.source import ClaudeCodeSource
from typer.testing import CliRunner


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def claude_code_app():
    source = ClaudeCodeSource()
    return source.get_commands()


class TestListCommand:
    """Test the `list` CLI command."""

    def test_lists_sessions(self, cli_runner, claude_code_app):
        sessions = [
            SessionSummary(
                session="aaaa1111-0000-0000-0000-000000000001",
                path="/tmp/a.jsonl",
                modified="2026-04-06T10:00:00",
                size_kb=200,
                project="lestash",
                title="review auth middleware",
            ),
            SessionSummary(
                session="bbbb2222-0000-0000-0000-000000000002",
                path="/tmp/b.jsonl",
                modified="2026-04-05T10:00:00",
                size_kb=50,
                project="atomicguard",
                title=None,
            ),
        ]

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=sessions),
        ):
            result = cli_runner.invoke(claude_code_app, ["list"])

        assert result.exit_code == 0
        assert "lestash" in result.stdout
        assert "atomicguard" in result.stdout
        assert "2 of 2 sessions" in result.stdout

    def test_filters_by_project(self, cli_runner, claude_code_app):
        sessions = [
            SessionSummary(
                session="aaaa1111",
                path="/tmp/a.jsonl",
                modified="2026-04-06T10:00:00",
                size_kb=200,
                project="lestash",
                title="test",
            ),
            SessionSummary(
                session="bbbb2222",
                path="/tmp/b.jsonl",
                modified="2026-04-05T10:00:00",
                size_kb=100,
                project="other",
                title="other test title here",
            ),
        ]

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=sessions),
        ):
            result = cli_runner.invoke(claude_code_app, ["list", "--project", "lestash"])

        assert result.exit_code == 0
        assert "1 of 1 sessions" in result.stdout

    def test_exits_when_reveal_missing(self, cli_runner, claude_code_app):
        with patch("lestash_claude_code.source.check_reveal", return_value=False):
            result = cli_runner.invoke(claude_code_app, ["list"])

        assert result.exit_code == 1
        assert "Reveal CLI not found" in result.stdout

    def test_xml_titles_shown_as_auto_generated(self, cli_runner, claude_code_app):
        sessions = [
            SessionSummary(
                session="aaaa1111",
                path="/tmp/a.jsonl",
                modified="2026-04-06T10:00:00",
                size_kb=200,
                project="lestash",
                title="<local-command-caveat>Caveat...",
            ),
        ]

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=sessions),
        ):
            result = cli_runner.invoke(claude_code_app, ["list"])

        assert result.exit_code == 0
        assert "(auto-generated)" in result.stdout


class TestSyncCommand:
    """Test the `sync` CLI command."""

    def test_exits_when_reveal_missing(self, cli_runner, claude_code_app):
        with patch("lestash_claude_code.source.check_reveal", return_value=False):
            result = cli_runner.invoke(claude_code_app, ["sync"])

        assert result.exit_code == 1
        assert "Reveal CLI not found" in result.stdout

    def test_reports_no_matching_sessions(self, cli_runner, claude_code_app):
        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=[]),
        ):
            result = cli_runner.invoke(claude_code_app, ["sync"])

        assert result.exit_code == 0
        assert "No sessions match" in result.stdout


class TestStatusCommand:
    """Test the `status` CLI command."""

    def test_shows_zero_when_empty(self, cli_runner, claude_code_app, tmp_path):
        from lestash.core.config import Config, GeneralConfig
        from lestash.core.database import init_database

        db_path = tmp_path / "test.db"
        config = Config(general=GeneralConfig(database_path=str(db_path)))
        init_database(config)

        with patch("lestash.core.config.Config.load", return_value=config):
            result = cli_runner.invoke(claude_code_app, ["status"])

        assert result.exit_code == 0
        assert "0" in result.stdout


class TestSyncMethod:
    """Test the SourcePlugin.sync() iterator method."""

    def test_yields_items(self, session_summary_factory, session_detail_factory):
        source = ClaudeCodeSource()
        summary = session_summary_factory(size_kb=100)
        detail = session_detail_factory()

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=[summary]),
            patch("lestash_claude_code.source.get_session_detail", return_value=detail),
            patch(
                "lestash_claude_code.source.extract_first_substantive_message",
                return_value="test message here",
            ),
        ):
            items = list(source.sync({"min_size_kb": 50}))

        assert len(items) == 1
        assert items[0].source_type == "claude-code"
        assert items[0].source_id == summary.session

    def test_filters_by_min_size(self, session_summary_factory):
        source = ClaudeCodeSource()
        small = session_summary_factory(session="small", size_kb=10)
        large = session_summary_factory(session="large", size_kb=200)

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=[small, large]),
            patch(
                "lestash_claude_code.source.get_session_detail",
                return_value=SessionDetail(session="large"),
            ),
            patch(
                "lestash_claude_code.source.extract_first_substantive_message",
                return_value=None,
            ),
        ):
            items = list(source.sync({"min_size_kb": 50}))

        assert len(items) == 1
        assert items[0].source_id == "large"

    def test_raises_when_reveal_missing(self):
        source = ClaudeCodeSource()

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=False),
            pytest.raises(RuntimeError, match="Reveal CLI not found"),
        ):
            list(source.sync({}))

    def test_skips_failed_sessions(self, session_summary_factory):
        source = ClaudeCodeSource()
        s1 = session_summary_factory(session="good", size_kb=100)
        s2 = session_summary_factory(session="bad", size_kb=100)

        def mock_detail(session_id):
            if session_id == "bad":
                raise RuntimeError("reveal subprocess failed")
            return SessionDetail(session=session_id)

        with (
            patch("lestash_claude_code.source.check_reveal", return_value=True),
            patch("lestash_claude_code.source.list_sessions", return_value=[s1, s2]),
            patch("lestash_claude_code.source.get_session_detail", side_effect=mock_detail),
            patch(
                "lestash_claude_code.source.extract_first_substantive_message",
                return_value=None,
            ),
        ):
            items = list(source.sync({"min_size_kb": 50}))

        assert len(items) == 1
        assert items[0].source_id == "good"

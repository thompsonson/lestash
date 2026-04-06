"""Test fixtures for Claude Code plugin tests."""

import json

import pytest
from lestash_claude_code.client import SessionDetail, SessionSummary


@pytest.fixture
def session_summary_factory():
    """Factory to create SessionSummary objects with sensible defaults."""

    def _create(
        session: str = "bc73cfe4-7fd3-4f4a-b17d-3a9480c1f2f0",
        path: str = "/home/user/.claude/projects/-home-user-myproject/bc73cfe4.jsonl",
        modified: str = "2026-04-05T09:03:36.546401",
        size_kb: int = 235,
        project: str = "lestash",
        title: str | None = None,
    ) -> SessionSummary:
        return SessionSummary(
            session=session,
            path=path,
            modified=modified,
            size_kb=size_kb,
            project=project,
            title=title,
        )

    return _create


@pytest.fixture
def session_detail_factory():
    """Factory to create SessionDetail objects with sensible defaults."""

    def _create(
        session: str = "bc73cfe4-7fd3-4f4a-b17d-3a9480c1f2f0",
        title: str | None = None,
        message_count: int = 57,
        user_messages: int = 32,
        assistant_messages: int = 15,
        tools_used: dict | None = None,
        files_touched: list | None = None,
        files_touched_count: int = 0,
        duration: str = "9m 56s",
        token_summary: dict | None = None,
        cwd: str | None = "/home/user/Projects/myproject",
        git_branch: str | None = "main",
        version: str | None = "2.1.87",
        tool_details: dict | None = None,
        file_operations: dict | None = None,
    ) -> SessionDetail:
        return SessionDetail(
            session=session,
            title=title,
            message_count=message_count,
            user_messages=user_messages,
            assistant_messages=assistant_messages,
            tools_used=tools_used or {"Bash": 6, "Agent": 3},
            files_touched=files_touched or [],
            files_touched_count=files_touched_count,
            duration=duration,
            token_summary=token_summary or {},
            cwd=cwd,
            git_branch=git_branch,
            version=version,
            tool_details=tool_details or {},
            file_operations=file_operations or {},
        )

    return _create


@pytest.fixture
def reveal_sessions_response():
    """Sample JSON response from `reveal claude://sessions/ --format json --all`."""
    return {
        "contract_version": "1.0",
        "type": "claude_session_list",
        "source": "/home/user/.claude/projects",
        "session_count": 3,
        "recent_sessions": [
            {
                "session": "aaaa1111-0000-0000-0000-000000000001",
                "path": "/home/user/.claude/projects/-proj-a/aaaa1111.jsonl",
                "modified": "2026-04-06T17:16:37.814732",
                "size_kb": 156,
                "project": "lestash",
                "title": "review the auth middleware",
            },
            {
                "session": "bbbb2222-0000-0000-0000-000000000002",
                "path": "/home/user/.claude/projects/-proj-b/bbbb2222.jsonl",
                "modified": "2026-04-05T09:00:00.000000",
                "size_kb": 30,
                "project": "atomicguard",
                "title": "<local-command-caveat>Caveat: messages...",
            },
            {
                "session": "cccc3333-0000-0000-0000-000000000003",
                "path": "/home/user/.claude/projects/-proj-c/cccc3333.jsonl",
                "modified": "2026-04-04T12:00:00.000000",
                "size_kb": 500,
                "project": "lestash",
                "title": None,
            },
        ],
    }


@pytest.fixture
def reveal_session_detail_response():
    """Sample JSON response from `reveal claude://session/<uuid> --format json`."""
    return {
        "contract_version": "1.0",
        "type": "claude_session_overview",
        "session": "aaaa1111-0000-0000-0000-000000000001",
        "title": "review the auth middleware",
        "message_count": 109,
        "user_messages": 47,
        "assistant_messages": 51,
        "tools_used": {"Bash": 17, "Grep": 8, "WebFetch": 6, "Read": 2},
        "files_touched": ["src/auth.py", "tests/test_auth.py"],
        "files_touched_count": 2,
        "duration": "8m 30s",
        "token_summary": {
            "input_tokens": 1200,
            "output_tokens": 3400,
            "cache_read_tokens": 50000,
        },
        "context": {
            "cwd": "/home/user/Projects/lestash",
            "git_branch": "feat/auth",
            "version": "2.1.87",
        },
    }


@pytest.fixture
def reveal_tools_response():
    """Sample JSON response from `reveal claude://session/<uuid>/tools --format json`."""
    return {
        "contract_version": "1.0",
        "type": "claude_tool_summary",
        "session": "aaaa1111-0000-0000-0000-000000000001",
        "total_calls": 33,
        "tools": {
            "Bash": {"count": 17, "success_rate": "85.2%", "success": 14, "failure": 3},
            "Read": {"count": 10, "success_rate": "100.0%", "success": 10, "failure": 0},
            "Edit": {"count": 6, "success_rate": "83.3%", "success": 5, "failure": 1},
        },
    }


@pytest.fixture
def reveal_files_response():
    """Sample JSON response from `reveal claude://session/<uuid>/files --format json`."""
    return {
        "contract_version": "1.0",
        "type": "claude_files",
        "session": "aaaa1111-0000-0000-0000-000000000001",
        "total_operations": 12,
        "unique_files": 3,
        "by_operation": {
            "Read": {"src/auth.py": 3, "tests/test_auth.py": 1},
            "Write": {"src/auth.py": 1},
            "Edit": {"src/auth.py": 2, "tests/test_auth.py": 1},
        },
    }


@pytest.fixture
def sample_jsonl_file(tmp_path):
    """Create a sample JSONL session file and return its path.

    Returns a factory that accepts a list of (type, content) tuples.
    """

    def _create(messages: list[tuple[str, str | list]] | None = None) -> str:
        if messages is None:
            messages = [
                ("system", "System initialization"),
                ("user", "<local-command-caveat>Caveat: generated by user..."),
                ("user", "<command-name>/clear</command-name>"),
                ("user", "yes"),
                ("user", "review the auth middleware for security issues"),
                ("assistant", "I'll review the auth middleware."),
            ]

        path = tmp_path / "session.jsonl"
        with open(path, "w") as f:
            for msg_type, content in messages:
                if isinstance(content, list):
                    entry = {
                        "type": msg_type,
                        "message": {"content": content},
                    }
                else:
                    entry = {"type": msg_type, "content": content}
                f.write(json.dumps(entry) + "\n")

        return str(path)

    return _create

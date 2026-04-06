"""Reveal CLI wrapper and JSONL parser for Claude Code sessions."""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionSummary:
    """Session metadata from reveal claude://sessions/ list."""

    session: str
    path: str
    modified: str
    size_kb: int
    project: str
    title: str | None = None


@dataclass
class SessionDetail:
    """Session overview from reveal claude://session/<uuid>."""

    session: str
    title: str | None = None
    message_count: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tools_used: dict[str, int] = field(default_factory=dict)
    files_touched: list[str] = field(default_factory=list)
    files_touched_count: int = 0
    duration: str = ""
    token_summary: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    tool_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    file_operations: dict[str, dict[str, int]] = field(default_factory=dict)


def _run_reveal(args: list[str], timeout: int = 30) -> dict:
    """Run a reveal command and return parsed JSON output.

    Raises RuntimeError if reveal fails or returns non-JSON output.
    """
    cmd = ["reveal", *args, "--format", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"reveal failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def check_reveal() -> bool:
    """Check if reveal is installed and available."""
    try:
        subprocess.run(["reveal", "--version"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return False
    return True


def list_sessions(since: str | None = None) -> list[SessionSummary]:
    """List all Claude Code sessions via Reveal.

    Args:
        since: Optional ISO date string (YYYY-MM-DD) to filter sessions.

    Returns:
        List of SessionSummary objects, sorted by modified date (newest first).
    """
    args = ["claude://sessions/"]
    if since:
        args.extend(["--since", since])
    else:
        args.append("--all")

    data = _run_reveal(args, timeout=60)
    sessions = []
    for entry in data.get("recent_sessions", []):
        sessions.append(
            SessionSummary(
                session=entry["session"],
                path=entry["path"],
                modified=entry["modified"],
                size_kb=entry.get("size_kb", 0),
                project=entry.get("project", "unknown"),
                title=entry.get("title"),
            )
        )
    return sessions


def _get_tool_details(session_id: str) -> dict[str, dict[str, Any]]:
    """Fetch per-tool success rates from the /tools subview."""
    try:
        data = _run_reveal([f"claude://session/{session_id}/tools"])
        return data.get("tools", {})
    except (RuntimeError, json.JSONDecodeError):
        logger.warning("Could not fetch tool details for session %s", session_id)
        return {}


def _get_file_operations(session_id: str) -> dict[str, dict[str, int]]:
    """Fetch file operation breakdown from the /files subview."""
    try:
        data = _run_reveal([f"claude://session/{session_id}/files"])
        return data.get("by_operation", {})
    except (RuntimeError, json.JSONDecodeError):
        logger.warning("Could not fetch file operations for session %s", session_id)
        return {}


def get_session_detail(session_id: str) -> SessionDetail:
    """Get detailed overview of a single session.

    Args:
        session_id: Session UUID.

    Returns:
        SessionDetail with message counts, tools, duration, etc.
    """
    data = _run_reveal([f"claude://session/{session_id}"])
    context = data.get("context", {})
    tool_details = _get_tool_details(session_id)
    file_operations = _get_file_operations(session_id)
    return SessionDetail(
        session=data.get("session", session_id),
        title=data.get("title"),
        message_count=data.get("message_count", 0),
        user_messages=data.get("user_messages", 0),
        assistant_messages=data.get("assistant_messages", 0),
        tools_used=data.get("tools_used", {}),
        files_touched=data.get("files_touched", []),
        files_touched_count=data.get("files_touched_count", 0),
        duration=data.get("duration", ""),
        token_summary=data.get("token_summary", {}),
        cwd=context.get("cwd"),
        git_branch=context.get("git_branch"),
        version=context.get("version"),
        tool_details=tool_details,
        file_operations=file_operations,
    )


def extract_first_substantive_message(jsonl_path: str) -> str | None:
    """Extract the first substantive user message from a session JSONL file.

    Walks user messages in order and returns the first that:
    1. Does not start with '<' (XML/system tags)
    2. Is at least 20 characters long
    3. Contains more than one word

    Returns the message truncated to 200 characters, or None if no message qualifies.
    """
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "user":
                    continue

                text = _extract_text(entry)
                if not text:
                    continue

                text = text.strip()
                if text.startswith("<"):
                    continue
                if len(text) < 20:
                    continue
                if " " not in text.strip():
                    continue

                return text[:200]
    except OSError:
        logger.warning("Could not read JSONL file: %s", jsonl_path)

    return None


def _extract_text(entry: dict) -> str | None:
    """Extract text content from a JSONL message entry.

    Handles two formats:
    - Top-level `content` string
    - `message.content` as a list of {type: "text", text: "..."} blocks
    """
    # Try message.content (newer format)
    message = entry.get("message", {})
    if message:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return " ".join(parts) if parts else None

    # Try top-level content (older format)
    content = entry.get("content")
    if isinstance(content, str):
        return content

    return None

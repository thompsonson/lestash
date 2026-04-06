"""Claude Code session source plugin implementation."""

import json
import os
import socket
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any

import typer
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console
from rich.table import Table

from lestash_claude_code.client import (
    SessionDetail,
    SessionSummary,
    check_reveal,
    extract_first_substantive_message,
    get_session_detail,
    list_sessions,
)

console = Console()


def clean_title(raw: str | None, project: str, branch: str | None, modified: str) -> str:
    """Clean a session title using the 5-rule cleanup.

    1. If starts with '<', discard and use fallback
    2. Strip whitespace, truncate at first newline
    3. If empty or under 5 chars, use fallback
    4. If exceeds 120 chars, truncate with '…'
    5. Fallback: "{project} — {branch} ({YYYY-MM-DD})"
    """
    date_str = modified[:10] if len(modified) >= 10 else modified
    fallback = f"{project} — {branch} ({date_str})" if branch else f"{project} ({date_str})"

    if not raw or raw.startswith("<"):
        return fallback

    title = raw.strip().split("\n")[0].strip()

    if len(title) < 5:
        return fallback

    if len(title) > 120:
        return title[:119] + "…"

    return title


def build_content(
    first_message: str | None,
    duration: str,
    project: str,
    branch: str | None,
    files_touched: list[str],
    files_touched_count: int,
    tools_used: dict[str, int],
    message_count: int,
    user_messages: int,
    assistant_messages: int,
) -> str:
    """Build human-readable content for FTS5 indexing."""
    lines: list[str] = []

    if first_message:
        lines.append(first_message)
        lines.append("")

    branch_part = f" ({branch} branch)" if branch else ""
    lines.append(f"{duration} session on {project}{branch_part}.")

    if files_touched:
        basenames = ", ".join(os.path.basename(f) for f in files_touched)
        lines.append(f"Touched {len(files_touched)} files: {basenames}.")
    elif files_touched_count > 0:
        lines.append(f"Touched {files_touched_count} files.")

    if tools_used:
        tool_parts = [f"{tool} ({count})" for tool, count in tools_used.items()]
        lines.append(f"Tools: {', '.join(tool_parts)}.")

    lines.append(
        f"{message_count} messages ({user_messages} user, {assistant_messages} assistant)."
    )

    return "\n".join(lines)


def session_to_item(
    summary: SessionSummary,
    detail: SessionDetail,
    first_message: str | None,
) -> ItemCreate:
    """Convert session data to an ItemCreate."""
    title = clean_title(
        detail.title or summary.title,
        summary.project,
        detail.git_branch,
        summary.modified,
    )
    content = build_content(
        first_message=first_message,
        duration=detail.duration,
        project=summary.project,
        branch=detail.git_branch,
        files_touched=detail.files_touched,
        files_touched_count=detail.files_touched_count,
        tools_used=detail.tools_used,
        message_count=detail.message_count,
        user_messages=detail.user_messages,
        assistant_messages=detail.assistant_messages,
    )

    created_at = datetime.fromisoformat(summary.modified)

    metadata: dict[str, Any] = {
        "project": summary.project,
        "branch": detail.git_branch,
        "duration": detail.duration,
        "message_count": detail.message_count,
        "tools_used": detail.tools_used,
        "tool_details": detail.tool_details,
        "file_operations": detail.file_operations,
        "files_touched": detail.files_touched,
        "token_summary": detail.token_summary,
        "size_kb": summary.size_kb,
        "hostname": socket.gethostname(),
        "cwd": detail.cwd,
        "claude_code_version": detail.version,
    }

    return ItemCreate(
        source_type="claude-code",
        source_id=summary.session,
        url=None,
        title=title,
        content=content,
        author="user",
        created_at=created_at,
        is_own_content=True,
        metadata=metadata,
    )


def _require_reveal() -> None:
    """Check that reveal is available, exit with clear error if not."""
    if not check_reveal():
        console.print(
            "[red]Reveal CLI not found.[/red] "
            "Install it with: [bold]pip install reveal-cli[/bold] "
            "or see https://github.com/Semantic-Infrastructure-Lab/reveal"
        )
        raise typer.Exit(1)


def _filter_sessions(
    sessions: list[SessionSummary],
    project: str | None = None,
    min_size: int = 50,
) -> list[SessionSummary]:
    """Apply project and size filters to session list."""
    filtered = []
    for s in sessions:
        if min_size > 0 and s.size_kb < min_size:
            continue
        if project and s.project.lower() != project.lower():
            continue
        filtered.append(s)
    return filtered


class ClaudeCodeSource(SourcePlugin):
    """Claude Code session history source plugin."""

    name = "claude-code"
    description = "Claude Code session history"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with claude-code commands."""
        app = typer.Typer(help="Claude Code session import commands.")

        @app.command("sync")
        def sync_cmd(
            project: Annotated[
                str | None,
                typer.Option("--project", "-p", help="Filter by project name"),
            ] = None,
            since: Annotated[
                str | None,
                typer.Option("--since", "-s", help="Only sessions after this date (YYYY-MM-DD)"),
            ] = None,
            min_size: Annotated[
                int,
                typer.Option("--min-size", help="Minimum session size in KB"),
            ] = 50,
        ) -> None:
            """Import Claude Code sessions into the knowledge base."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            _require_reveal()

            console.print("[bold]Fetching session list...[/bold]")
            sessions = list_sessions(since=since)
            filtered = _filter_sessions(sessions, project=project, min_size=min_size)

            if not filtered:
                console.print(
                    f"[dim]No sessions match filters "
                    f"(total: {len(sessions)}, min_size: {min_size}KB"
                    f"{f', project: {project}' if project else ''}).[/dim]"
                )
                return

            console.print(
                f"[bold]Syncing {len(filtered)} sessions "
                f"(filtered from {len(sessions)} total)...[/bold]"
            )

            config = Config.load()
            synced = 0
            errors = 0

            with get_connection(config) as conn:
                for i, summary in enumerate(filtered, 1):
                    try:
                        detail = get_session_detail(summary.session)
                        first_msg = extract_first_substantive_message(summary.path)
                        item = session_to_item(summary, detail, first_msg)

                        metadata_json = json.dumps(item.metadata) if item.metadata else None
                        conn.execute(
                            """
                            INSERT INTO items (
                                source_type, source_id, url, title, content,
                                author, created_at, is_own_content, metadata, parent_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(source_type, source_id) DO UPDATE SET
                                title = excluded.title,
                                content = excluded.content,
                                author = excluded.author,
                                is_own_content = excluded.is_own_content,
                                metadata = excluded.metadata,
                                parent_id = excluded.parent_id
                            """,
                            (
                                item.source_type,
                                item.source_id,
                                item.url,
                                item.title,
                                item.content,
                                item.author,
                                item.created_at,
                                item.is_own_content,
                                metadata_json,
                                item.parent_id,
                            ),
                        )
                        synced += 1

                        if i % 10 == 0 or i == len(filtered):
                            console.print(
                                f"  [dim]Progress: {i}/{len(filtered)}[/dim]",
                                highlight=False,
                            )
                    except Exception as e:
                        errors += 1
                        self.logger.warning("Failed to sync session %s: %s", summary.session, e)

                conn.commit()

            console.print(
                f"[green]Synced {synced} sessions[/green]"
                + (f" [yellow]({errors} errors)[/yellow]" if errors else "")
            )

        @app.command("status")
        def status_cmd() -> None:
            """Show import status for Claude Code sessions."""
            from lestash.core.config import Config
            from lestash.core.database import get_connection

            config = Config.load()
            with get_connection(config) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM items WHERE source_type = 'claude-code'"
                ).fetchone()[0]

                projects = conn.execute(
                    "SELECT DISTINCT json_extract(metadata, '$.project') as project "
                    "FROM items WHERE source_type = 'claude-code' ORDER BY project"
                ).fetchall()

                last_sync = conn.execute(
                    "SELECT last_sync FROM sources WHERE source_type = 'claude-code'"
                ).fetchone()

            console.print(f"[bold]Claude Code sessions:[/bold] {total}")
            if projects:
                project_names = ", ".join(row[0] for row in projects if row[0])
                console.print(f"[bold]Projects:[/bold] {project_names}")
            if last_sync and last_sync[0]:
                console.print(f"[bold]Last sync:[/bold] {last_sync[0]}")

        @app.command("list")
        def list_cmd(
            project: Annotated[
                str | None,
                typer.Option("--project", "-p", help="Filter by project"),
            ] = None,
            limit: Annotated[
                int,
                typer.Option("--limit", "-n", help="Max results to show"),
            ] = 20,
        ) -> None:
            """List available Claude Code sessions from Reveal (without importing)."""
            _require_reveal()

            sessions = list_sessions()
            if project:
                sessions = [s for s in sessions if s.project.lower() == project.lower()]

            table = Table(show_header=True, header_style="bold")
            table.add_column("Session", max_width=12)
            table.add_column("Project")
            table.add_column("Modified")
            table.add_column("Size (KB)", justify="right")
            table.add_column("Title", max_width=50)

            for session in sessions[:limit]:
                title = session.title or ""
                if title.startswith("<"):
                    title = "[dim](auto-generated)[/dim]"
                elif len(title) > 50:
                    title = title[:49] + "…"

                table.add_row(
                    session.session[:8] + "…",
                    session.project,
                    session.modified[:16],
                    str(session.size_kb),
                    title,
                )

            console.print(table)
            shown = min(limit, len(sessions))
            console.print(f"[dim]Showing {shown} of {len(sessions)} sessions[/dim]")

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Yield ItemCreate for sessions matching config filters.

        Used by `lestash sources sync claude-code`.
        Config keys: min_size_kb (default 50).
        """
        if not check_reveal():
            raise RuntimeError("Reveal CLI not found. Install it with: pip install reveal-cli")

        min_size = config.get("min_size_kb", 50)
        sessions = list_sessions()
        filtered = _filter_sessions(sessions, min_size=min_size)

        self.logger.info(
            "Found %d sessions (%d after size filter)",
            len(sessions),
            len(filtered),
        )

        for summary in filtered:
            try:
                detail = get_session_detail(summary.session)
                first_msg = extract_first_substantive_message(summary.path)
                yield session_to_item(summary, detail, first_msg)
            except Exception as e:
                self.logger.warning("Failed to process session %s: %s", summary.session, e)

    def configure(self) -> dict:
        """Return default configuration."""
        return {"min_size_kb": 50}

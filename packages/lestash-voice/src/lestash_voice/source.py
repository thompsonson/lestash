"""Voice note source plugin implementation."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import typer
from lestash.models.item import ItemCreate
from lestash.plugins.base import SourcePlugin
from rich.console import Console

console = Console()

SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".webm"}


def _transcribe_and_save(
    file_path: Path,
    model: str,
    title: str | None,
) -> None:
    """Transcribe an audio file and save the result to the database."""
    from lestash.core.config import Config
    from lestash.core.database import get_connection

    from lestash_voice.transcribe import transcribe_file

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        console.print(
            f"[red]Unsupported format: {file_path.suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}[/red]"
        )
        raise typer.Exit(1)

    console.print(f"[dim]Transcribing {file_path.name} with model '{model}'...[/dim]")

    result = transcribe_file(file_path, model_name=model)

    if not result.text:
        console.print("[yellow]No speech detected in audio.[/yellow]")
        raise typer.Exit(1)

    source_id = f"voice-file-{int(time.time())}-{file_path.stem}"
    item = ItemCreate(
        source_type="voice",
        source_id=source_id,
        title=title or f"Voice note: {file_path.name}",
        content=result.text,
        is_own_content=True,
        metadata={
            "duration_seconds": result.duration_seconds,
            "model": result.model,
            "language": result.language,
            "original_filename": file_path.name,
            "input_type": "file",
        },
    )

    config = Config.load()
    with get_connection(config) as conn:
        metadata_json = json.dumps(item.metadata) if item.metadata else None
        conn.execute(
            """
            INSERT INTO items (
                source_type, source_id, url, title, content,
                author, created_at, is_own_content, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                title = excluded.title,
                content = excluded.content,
                metadata = excluded.metadata
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
            ),
        )
        conn.commit()

    console.print(f"[green]Saved voice note: {item.title}[/green]")
    console.print(f"[dim]Duration: {result.duration_seconds}s | Language: {result.language}[/dim]")
    console.print(f"\n{result.text[:200]}{'...' if len(result.text) > 200 else ''}")


class VoiceSource(SourcePlugin):
    """Voice note transcription plugin."""

    name = "voice"
    description = "Voice note transcription via Whisper"

    def get_commands(self) -> typer.Typer:
        """Return Typer app with voice commands."""
        app = typer.Typer(help="Voice note commands.")

        @app.command("note")
        def note_cmd(
            file: Annotated[
                Path,
                typer.Option("--file", "-f", help="Audio file to transcribe (m4a, mp3, wav)"),
            ],
            model: Annotated[
                str,
                typer.Option("--model", "-m", help="Whisper model size"),
            ] = "base.en",
            title: Annotated[
                str | None,
                typer.Option("--title", "-t", help="Custom title for the note"),
            ] = None,
        ) -> None:
            """Transcribe an audio file and save as a voice note."""
            if not file.exists():
                console.print(f"[red]File not found: {file}[/red]")
                raise typer.Exit(1)

            _transcribe_and_save(file, model=model, title=title)

        @app.command("status")
        def status_cmd() -> None:
            """Show Whisper model availability."""
            from lestash_voice.transcribe import get_model_path

            model_dir = get_model_path()
            console.print(f"[bold]Model directory:[/bold] {model_dir}")

            models = list(model_dir.iterdir()) if model_dir.exists() else []
            if models:
                console.print(f"[green]Downloaded models ({len(models)}):[/green]")
                for m in sorted(models):
                    if m.is_dir():
                        console.print(f"  - {m.name}")
            else:
                console.print(
                    "[yellow]No models downloaded yet."
                    " First transcription will auto-download.[/yellow]"
                )

        return app

    def sync(self, config: dict) -> Iterator[ItemCreate]:
        """Voice plugin does not support sync — notes are created on demand."""
        return iter([])

    def configure(self) -> dict:
        """Default configuration."""
        return {"model": "base.en"}

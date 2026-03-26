"""Whisper transcription using faster-whisper."""

import logging
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "base.en"
MODEL_DIR = Path.home() / ".local" / "share" / "lestash" / "whisper-models"


@dataclass
class TranscriptionResult:
    """Result of a Whisper transcription."""

    text: str
    language: str
    duration_seconds: float
    model: str


def get_model_path() -> Path:
    """Return the model cache directory, creating it if needed."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return MODEL_DIR


def transcribe_file(
    file_path: str | Path,
    model_name: str = DEFAULT_MODEL,
    device: str = "auto",
) -> TranscriptionResult:
    """Transcribe an audio file using faster-whisper.

    Args:
        file_path: Path to audio file (m4a, mp3, wav, etc.).
        model_name: Whisper model size (tiny, base, small, medium, large-v3).
        device: Compute device — "auto", "cpu", or "cuda".

    Returns:
        TranscriptionResult with text, language, duration, and model used.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        msg = f"Audio file not found: {file_path}"
        raise FileNotFoundError(msg)

    logger.info("Loading whisper model '%s' (device=%s)", model_name, device)
    model = WhisperModel(
        model_name,
        device=device,
        download_root=str(get_model_path()),
    )

    logger.info("Transcribing %s", file_path.name)
    segments, info = model.transcribe(str(file_path))

    text_parts = [segment.text for segment in segments]
    full_text = " ".join(text_parts).strip()

    logger.info(
        "Transcription complete: %.1fs audio, language=%s",
        info.duration,
        info.language,
    )

    return TranscriptionResult(
        text=full_text,
        language=info.language,
        duration_seconds=round(info.duration, 2),
        model=model_name,
    )

"""Tests for voice transcription module."""

from unittest.mock import MagicMock, patch

import pytest
from lestash_voice.transcribe import TranscriptionResult, transcribe_file


class TestTranscribeFile:
    """Test transcribe_file function."""

    def test_file_not_found(self, tmp_path):
        """Should raise FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError, match="Audio file not found"):
            transcribe_file(tmp_path / "nonexistent.mp3")

    @patch("lestash_voice.transcribe.WhisperModel")
    def test_transcribe_success(self, mock_model_cls, tmp_path):
        """Should return transcription result from Whisper."""
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake audio")

        # Mock model and segments
        mock_segment_1 = MagicMock()
        mock_segment_1.text = "Hello world."
        mock_segment_2 = MagicMock()
        mock_segment_2.text = "This is a test."

        mock_info = MagicMock()
        mock_info.duration = 5.5
        mock_info.language = "en"

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([mock_segment_1, mock_segment_2], mock_info)
        mock_model_cls.return_value = mock_model

        result = transcribe_file(audio_file, model_name="base.en")

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hello world. This is a test."
        assert result.language == "en"
        assert result.duration_seconds == 5.5
        assert result.model == "base.en"

        mock_model_cls.assert_called_once()
        mock_model.transcribe.assert_called_once_with(str(audio_file))

    @patch("lestash_voice.transcribe.WhisperModel")
    def test_transcribe_empty(self, mock_model_cls, tmp_path):
        """Should return empty text when no speech detected."""
        audio_file = tmp_path / "silence.wav"
        audio_file.write_bytes(b"fake silence")

        mock_info = MagicMock()
        mock_info.duration = 2.0
        mock_info.language = "en"

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([], mock_info)
        mock_model_cls.return_value = mock_model

        result = transcribe_file(audio_file)

        assert result.text == ""
        assert result.duration_seconds == 2.0


class TestVoiceSource:
    """Test VoiceSource plugin."""

    def test_plugin_attributes(self):
        from lestash_voice.source import VoiceSource

        plugin = VoiceSource()
        assert plugin.name == "voice"
        assert plugin.description

    def test_sync_yields_nothing(self):
        from lestash_voice.source import VoiceSource

        plugin = VoiceSource()
        items = list(plugin.sync({}))
        assert items == []

    def test_get_commands_returns_typer(self):
        from lestash_voice.source import VoiceSource

        plugin = VoiceSource()
        app = plugin.get_commands()
        assert app is not None

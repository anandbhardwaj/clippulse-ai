from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion import IngestionError, MediaIngestionService


@patch("yt_dlp.YoutubeDL")
def test_fetch_success(mock_ydl_cls, tmp_path):
    service = MediaIngestionService(work_dir=tmp_path)
    (tmp_path / "raw_video.mp4").write_text("dummy video")
    (tmp_path / "raw_audio.mp3").write_text("dummy audio")

    result = service.fetch("https://www.youtube.com/shorts/sample")
    assert Path(result["video_path"]).exists()
    assert Path(result["audio_path"]).exists()


@patch("yt_dlp.YoutubeDL")
def test_fetch_failure_raises_ingestion_error(mock_ydl_cls, tmp_path):
    mock_instance = MagicMock()
    mock_instance.download.side_effect = Exception("Network error")
    mock_ydl_cls.return_value.__enter__.return_value = mock_instance

    service = MediaIngestionService(work_dir=tmp_path)
    with pytest.raises(IngestionError):
        service.fetch("https://invalid-url.com")

from pathlib import Path

import yt_dlp

from src.config import settings


class IngestionError(Exception):
    pass


class MediaIngestionService:
    def __init__(self, work_dir: Path | None = None):
        self.work_dir = work_dir or settings.temp_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def fetch(self, url: str) -> dict[str, str]:
        video_target = self.work_dir / "raw_video.mp4"
        audio_target = self.work_dir / "raw_audio.mp3"

        video_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(video_target),
            "overwrites": True,
            "quiet": True,
            "no_warnings": True,
        }

        audio_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(self.work_dir / "raw_audio.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
            "overwrites": True,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                ydl.download([url])
            with yt_dlp.YoutubeDL(audio_opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            raise IngestionError(f"Failed to ingest media from {url}: {exc!s}") from exc

        if not video_target.exists() or not audio_target.exists():
            raise IngestionError(
                "Downloaded media artifacts could not be found on disk."
            )

        return {
            "video_path": str(video_target),
            "audio_path": str(audio_target),
        }

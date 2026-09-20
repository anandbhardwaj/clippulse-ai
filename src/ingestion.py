import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp

from src.config import settings

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_SHORTS_PATH = re.compile(r"^/shorts/[A-Za-z0-9_-]+/?$")
_REEL_PATH = re.compile(r"^/(?:[\w.]+/)?reels?/[\w-]+/?$")

_BACKOFF_BASE_S = 2.0
_BACKOFF_CAP_S = 30.0
_FFMPEG_TIMEOUT_S = 300.0
_DURATION_TOLERANCE_S = 1.0
_SUPPORTED_HINT = (
    "Only YouTube Shorts and Instagram Reels are supported, e.g. "
    "https://www.youtube.com/shorts/<id> or https://www.instagram.com/reel/<id>/"
)


class Platform(StrEnum):
    YOUTUBE_SHORTS = "youtube_shorts"
    INSTAGRAM_REEL = "instagram_reel"


class UnavailableReason(StrEnum):
    PRIVATE = "private"
    LOGIN_REQUIRED = "login_required"
    REMOVED = "removed"
    GEO_BLOCKED = "geo_blocked"


class IngestionError(Exception):
    pass


class UnsupportedUrlError(IngestionError):
    pass


class MediaUnavailableError(IngestionError):
    def __init__(self, message: str, reason: UnavailableReason):
        super().__init__(message)
        self.reason = reason


class IngestionNetworkError(IngestionError):
    pass


class IngestionRateLimitedError(IngestionError):
    pass


class FFmpegNotFoundError(IngestionError):
    pass


class AudioExtractionError(IngestionError):
    pass


@dataclass(frozen=True, slots=True)
class IngestionResult:
    video_path: Path
    audio_path: Path
    duration_s: float
    platform: Platform


_UNAVAILABLE_RULES: tuple[tuple[UnavailableReason, str, tuple[str, ...]], ...] = (
    (
        UnavailableReason.PRIVATE,
        "the video is private",
        ("private video", "this video is private", "private account"),
    ),
    (
        UnavailableReason.LOGIN_REQUIRED,
        "the platform requires you to be logged in",
        ("sign in", "log in", "login required", "logged-in", "cookies"),
    ),
    (
        UnavailableReason.GEO_BLOCKED,
        "the video is not available in your region",
        (
            "in your country",
            "geo restrict",
            "geo-restrict",
        ),
    ),
    (
        UnavailableReason.REMOVED,
        "the video was removed or is unavailable",
        (
            "video unavailable",
            "has been removed",
            "no longer available",
            "video is not available",
            "content isn't available",
            "does not exist",
            "http error 404",
        ),
    ),
)
_NEXT_STEPS: dict[UnavailableReason, str] = {
    UnavailableReason.PRIVATE: "make the video public or use a public URL",
    UnavailableReason.LOGIN_REQUIRED: (
        "set YTDLP_COOKIES_FILE with a logged-in session"
    ),
    UnavailableReason.REMOVED: "check the link still opens in a browser",
    UnavailableReason.GEO_BLOCKED: "try from a supported region",
}
_RATE_LIMIT_MARKERS = (
    "http error 429",
    "too many requests",
    "rate limit",
    "rate-limit",
)
_NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "name resolution",
    "getaddrinfo",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "unable to download",
    "urlopen error",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
)


def _ffmpeg_install_hint() -> str:
    if sys.platform == "win32":
        return "winget install Gyan.FFmpeg"
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    return "sudo apt update && sudo apt install -y ffmpeg"


class MediaIngestionService:
    def __init__(
        self,
        work_dir: Path | None = None,
        *,
        downloader_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.work_dir = work_dir or settings.temp_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._downloader_factory = downloader_factory
        self._run = run
        self._which = which
        self.max_retries = (
            settings.ingest_max_retries if max_retries is None else max_retries
        )
        self._sleep = sleep

    @property
    def video_target(self) -> Path:
        return self.work_dir / "raw_video.mp4"

    @property
    def audio_target(self) -> Path:
        return self.work_dir / "raw_audio.mp3"

    def fetch(self, url: str) -> IngestionResult:
        platform = self._classify_url(url)
        self._require_ffmpeg()
        self._clean_workdir()

        try:
            info = self._download_video(url)
            self._verify_non_empty(self.video_target)
            duration_s = self._resolve_duration(info)
            self._extract_audio()
            self._verify_non_empty(self.audio_target)
            self._verify_audio(duration_s)
        except Exception:
            self._clean_workdir()
            raise

        return IngestionResult(
            video_path=self.video_target,
            audio_path=self.audio_target,
            duration_s=duration_s,
            platform=platform,
        )

    def _classify_url(self, url: str) -> Platform:
        if not isinstance(url, str) or not url.strip():
            raise UnsupportedUrlError(f"URL is empty. {_SUPPORTED_HINT}")
        try:
            parts = urlsplit(url.strip())
            host = parts.hostname
        except ValueError as exc:
            raise UnsupportedUrlError(
                f"Not a valid URL: {url!r}. {_SUPPORTED_HINT}"
            ) from exc

        if parts.scheme in {"http", "https"} and host:
            if host in _YOUTUBE_HOSTS and _SHORTS_PATH.match(parts.path):
                return Platform.YOUTUBE_SHORTS
            if host in _INSTAGRAM_HOSTS and _REEL_PATH.match(parts.path):
                return Platform.INSTAGRAM_REEL

        raise UnsupportedUrlError(f"Unsupported URL: {url!r}. {_SUPPORTED_HINT}")

    def _require_ffmpeg(self) -> None:
        for tool in ("ffmpeg", "ffprobe"):
            if not self._which(tool):
                raise FFmpegNotFoundError(
                    f"{tool} was not found on PATH. Install FFmpeg with: "
                    f"{_ffmpeg_install_hint()}"
                )

    def _clean_workdir(self) -> None:
        for pattern in ("raw_video*", "raw_audio*"):
            for stale in self.work_dir.glob(pattern):
                if stale.is_file():
                    stale.unlink(missing_ok=True)

    def _download_video(self, url: str) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                return self._download_once(url)
            except Exception as exc:
                error = self._translate_error(exc, url)
                retryable = isinstance(
                    error, IngestionRateLimitedError | IngestionNetworkError
                )
                if not retryable or attempt >= self.max_retries:
                    raise error from exc
                self._sleep(min(_BACKOFF_BASE_S * 2**attempt, _BACKOFF_CAP_S))
                attempt += 1

    def _download_once(self, url: str) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(self.video_target),
            "merge_output_format": "mp4",
            "overwrites": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": settings.ingest_timeout_s,
            "retries": 0,
        }
        if settings.ytdlp_cookies_file:
            opts["cookiefile"] = str(settings.ytdlp_cookies_file)

        with self._downloader_factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        return info if isinstance(info, dict) else {}

    def _translate_error(self, exc: Exception, url: str) -> IngestionError:
        raw = str(exc)
        lowered = raw.lower()

        if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
            return IngestionRateLimitedError(
                f"The platform is rate limiting requests for {url}. Next step: wait "
                "a few minutes and retry, or set YTDLP_COOKIES_FILE with a "
                "logged-in session."
            )
        for reason, description, markers in _UNAVAILABLE_RULES:
            if any(marker in lowered for marker in markers):
                return MediaUnavailableError(
                    f"Cannot download {url}: {description}. "
                    f"Next step: {_NEXT_STEPS[reason]}.",
                    reason,
                )
        if isinstance(exc, TimeoutError | ConnectionError) or any(
            marker in lowered for marker in _NETWORK_MARKERS
        ):
            return IngestionNetworkError(
                f"Network problem while downloading {url}. "
                f"Next step: check your connection and retry. Details: {raw}"
            )
        return IngestionError(f"Failed to ingest media from {url}: {raw}")

    def _resolve_duration(self, info: dict[str, Any]) -> float:
        duration = info.get("duration")
        if isinstance(duration, int | float) and duration > 0:
            return round(float(duration), 2)
        return round(self._probe(self.video_target)[1], 2)

    def _extract_audio(self) -> None:
        argv = [
            "ffmpeg",
            "-y",
            "-i",
            str(self.video_target),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "128k",
            str(self.audio_target),
        ]
        try:
            proc = self._run(
                argv,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_FFMPEG_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AudioExtractionError(f"FFmpeg could not be run: {exc!s}") from exc
        if proc.returncode != 0:
            raise AudioExtractionError(
                "FFmpeg failed to extract audio (does the video have an audio "
                f"track?): {self._last_line(proc.stderr)}"
            )

    def _verify_non_empty(self, path: Path) -> None:
        if not path.exists() or path.stat().st_size == 0:
            raise IngestionError(
                f"Downloaded media artifact {path.name} is missing or empty."
            )

    def _verify_audio(self, video_duration_s: float) -> None:
        codec, audio_duration_s = self._probe(self.audio_target)
        if codec != "mp3":
            raise AudioExtractionError(
                f"Extracted audio has codec {codec!r}, expected 'mp3'."
            )
        if abs(audio_duration_s - video_duration_s) > _DURATION_TOLERANCE_S:
            raise AudioExtractionError(
                f"Extracted audio is {audio_duration_s:.2f}s but the video is "
                f"{video_duration_s:.2f}s; extraction looks truncated."
            )

    def _probe(self, path: Path) -> tuple[str | None, float]:
        argv = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name:format=duration",
            "-of",
            "json",
            str(path),
        ]
        try:
            proc = self._run(
                argv,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_FFMPEG_TIMEOUT_S,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(self._last_line(proc.stderr))
            payload = json.loads(proc.stdout)
            streams = payload.get("streams") or [{}]
            return streams[0].get("codec_name"), float(payload["format"]["duration"])
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            raise AudioExtractionError(
                f"ffprobe could not inspect {path.name}: {exc!s}"
            ) from exc

    @staticmethod
    def _last_line(text: str | None) -> str:
        lines = [line for line in (text or "").strip().splitlines() if line.strip()]
        return lines[-1].strip() if lines else "no error output"

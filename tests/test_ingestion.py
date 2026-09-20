import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any, Self

import pytest

from src.config import settings
from src.ingestion import (
    AudioExtractionError,
    FFmpegNotFoundError,
    IngestionError,
    IngestionNetworkError,
    IngestionRateLimitedError,
    MediaIngestionService,
    MediaUnavailableError,
    Platform,
    UnavailableReason,
    UnsupportedUrlError,
)

SHORTS_URL = "https://www.youtube.com/shorts/abc123XYZ_-"
REEL_URL = "https://www.instagram.com/reel/C1a2B3c4D5e/"


class FakeDownloader:
    """Stands in for yt_dlp.YoutubeDL; writes the video file like the real one."""

    def __init__(self, *, duration: float | None = 58.4, errors: list | None = None):
        self.duration = duration
        self.errors = list(errors or [])
        self.urls: list[str] = []
        self.opts: list[dict[str, Any]] = []
        self.stale_present_at_start: list[bool] = []

    def __call__(self, opts: dict[str, Any]) -> FakeDownloader:
        self.opts.append(opts)
        self._target = Path(opts["outtmpl"])
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.urls.append(url)
        self.stale_present_at_start.append(self._target.exists())
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self._target.write_bytes(b"fake video")
        return {"duration": self.duration}


class FakeRun:
    """Stands in for subprocess.run for both ffmpeg and ffprobe."""

    def __init__(
        self,
        *,
        audio_codec: str = "mp3",
        audio_duration: float = 58.4,
        video_duration: float = 58.4,
        ffmpeg_returncode: int = 0,
        ffmpeg_stderr: str = "",
        audio_bytes: bytes = b"fake mp3",
    ):
        self.audio_codec = audio_codec
        self.audio_duration = audio_duration
        self.video_duration = video_duration
        self.ffmpeg_returncode = ffmpeg_returncode
        self.ffmpeg_stderr = ffmpeg_stderr
        self.audio_bytes = audio_bytes
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[0] == "ffmpeg":
            if self.ffmpeg_returncode == 0:
                Path(argv[-1]).write_bytes(self.audio_bytes)
            return subprocess.CompletedProcess(
                argv, self.ffmpeg_returncode, "", self.ffmpeg_stderr
            )
        is_audio = argv[-1].endswith(".mp3")
        payload = {
            "streams": [{"codec_name": self.audio_codec if is_audio else "h264"}],
            "format": {
                "duration": str(
                    self.audio_duration if is_audio else self.video_duration
                )
            },
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def make_service(
    tmp_path: Path,
    *,
    downloader: FakeDownloader | None = None,
    run: FakeRun | None = None,
    which=lambda name: f"/usr/bin/{name}",
    max_retries: int = 2,
):
    downloader = downloader or FakeDownloader()
    run = run or FakeRun()
    sleeps: list[float] = []
    service = MediaIngestionService(
        work_dir=tmp_path,
        downloader_factory=downloader,
        run=run,
        which=which,
        max_retries=max_retries,
        sleep=sleeps.append,
    )
    return service, downloader, run, sleeps


def test_ac1_happy_path_youtube_shorts(tmp_path):
    """AC1 - Happy path: YouTube Shorts."""
    service, downloader, run, _ = make_service(tmp_path)

    result = service.fetch(SHORTS_URL)

    assert (tmp_path / "raw_video.mp4").read_bytes() == b"fake video"
    assert (tmp_path / "raw_audio.mp3").exists()
    assert downloader.urls == [SHORTS_URL]  # platform contacted exactly once
    ffmpeg_argv = next(call for call in run.calls if call[0] == "ffmpeg")
    assert ["-acodec", "libmp3lame", "-b:a", "128k"] == ffmpeg_argv[5:9]
    assert any(call[0] == "ffprobe" and call[-1].endswith(".mp3") for call in run.calls)
    assert result.platform is Platform.YOUTUBE_SHORTS
    assert result.duration_s == 58.4


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/reel/C1a2B3c4D5e/",
        "https://instagram.com/reels/C1a2B3c4D5e/",
        "https://www.instagram.com/some.user/reel/C1a2B3c4D5e/",
        "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc123",
        "https://www.instagram.com/reel/C1a2B3c4D5e/?utm_source=ig_web_copy_link",
    ],
)
def test_ac2_instagram_reel_urls_accepted(tmp_path, url):
    """AC2 - Instagram Reel URLs accepted."""
    service, downloader, _, _ = make_service(tmp_path)

    result = service.fetch(url)

    assert result.platform is Platform.INSTAGRAM_REEL
    assert downloader.urls == [url]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "ftp://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/watch?v=abc123",
        "https://example.com/shorts/abc123",
        "https://youtube.com.evil.example/shorts/abc",
        "https://youtube.com@evil.example/shorts/abc",
        "https://www.instagram.com/p/C1a2B3c4D5e/",
        "https://[broken/shorts/abc",
    ],
)
def test_ac3_unsupported_urls_rejected_before_any_io(tmp_path, url):
    """AC3 - Unsupported or malformed URLs are rejected before any network call."""
    service, downloader, run, _ = make_service(tmp_path)

    with pytest.raises(UnsupportedUrlError, match="YouTube Shorts"):
        service.fetch(url)

    assert downloader.urls == []
    assert run.calls == []


@pytest.mark.parametrize(
    ("raw_message", "error_type", "next_step"),
    [
        (
            "ERROR: Private video. Sign in if you've been granted access",
            MediaUnavailableError,
            "make the video public or use a public URL",
        ),
        (
            "ERROR: Sign in to confirm you're not a bot. Use --cookies",
            MediaUnavailableError,
            "set YTDLP_COOKIES_FILE with a logged-in session",
        ),
        (
            "ERROR: Video unavailable. This video has been removed",
            MediaUnavailableError,
            "check the link still opens in a browser",
        ),
        (
            "ERROR: The uploader has not made this video available in your country",
            MediaUnavailableError,
            "try from a supported region",
        ),
        (
            "ERROR: Read timed out.",
            IngestionNetworkError,
            "check your connection and retry",
        ),
        (
            "ERROR: <urlopen error [Errno 11001] getaddrinfo failed>",
            IngestionNetworkError,
            "check your connection and retry",
        ),
    ],
)
def test_ac4_unavailable_or_unreachable_media_is_actionable(
    tmp_path, raw_message, error_type, next_step
):
    """AC4 - Private or unreachable media fails with an actionable message."""
    original = Exception(raw_message)
    service, _, _, _ = make_service(
        tmp_path, downloader=FakeDownloader(errors=[original] * 5), max_retries=0
    )

    with pytest.raises(error_type) as caught:
        service.fetch(SHORTS_URL)

    assert next_step in str(caught.value)
    assert caught.value.__cause__ is original


def test_ac4_unavailable_reason_is_exposed(tmp_path):
    """AC4 - MediaUnavailableError carries a machine-readable reason."""
    service, _, _, _ = make_service(
        tmp_path, downloader=FakeDownloader(errors=[Exception("Private video")])
    )

    with pytest.raises(MediaUnavailableError) as caught:
        service.fetch(SHORTS_URL)

    assert caught.value.reason is UnavailableReason.PRIVATE


def test_ac4_unrecognised_error_falls_back_to_generic_with_raw_message(tmp_path):
    """AC4 - anything unrecognised -> IngestionError including the raw message."""
    service, _, _, _ = make_service(
        tmp_path, downloader=FakeDownloader(errors=[Exception("Something odd")])
    )

    with pytest.raises(IngestionError) as caught:
        service.fetch(SHORTS_URL)

    assert type(caught.value) is IngestionError
    assert "Something odd" in str(caught.value)


def test_ac4_cookies_file_is_passed_to_downloader(tmp_path, monkeypatch):
    """AC4 - the YTDLP_COOKIES_FILE hint is backed by real configuration."""
    monkeypatch.setattr(settings, "ytdlp_cookies_file", Path("cookies.txt"))
    service, downloader, _, _ = make_service(tmp_path)

    service.fetch(REEL_URL)

    assert downloader.opts[0]["cookiefile"] == "cookies.txt"


def test_ac5_rate_limit_is_retried_with_backoff_then_succeeds(tmp_path):
    """AC5 - HTTP 429 twice, third attempt succeeds."""
    rate_limited = Exception("ERROR: HTTP Error 429: Too Many Requests")
    downloader = FakeDownloader(errors=[rate_limited, rate_limited])
    service, _, _, sleeps = make_service(tmp_path, downloader=downloader)

    result = service.fetch(SHORTS_URL)

    assert result.platform is Platform.YOUTUBE_SHORTS
    assert len(downloader.urls) == 3
    assert sleeps == [2.0, 4.0]


def test_ac5_rate_limit_exhaustion_raises_actionable_error(tmp_path):
    """AC5 - HTTP 429 forever -> IngestionRateLimitedError after max retries."""
    rate_limited = Exception("ERROR: HTTP Error 429: Too Many Requests")
    downloader = FakeDownloader(errors=[rate_limited] * 10)
    service, _, _, sleeps = make_service(tmp_path, downloader=downloader)

    with pytest.raises(IngestionRateLimitedError, match="wait a few minutes"):
        service.fetch(SHORTS_URL)

    assert len(downloader.urls) == 3  # first try + 2 retries
    assert sleeps == [2.0, 4.0]


def test_ac5_transient_5xx_is_retried_as_network_error(tmp_path):
    """AC5 - transient 5xx follows the same bounded retry."""
    server_error = Exception("ERROR: HTTP Error 503: Service Unavailable")
    downloader = FakeDownloader(errors=[server_error] * 10)
    service, _, _, _ = make_service(tmp_path, downloader=downloader)

    with pytest.raises(IngestionNetworkError):
        service.fetch(SHORTS_URL)

    assert len(downloader.urls) == 3


def test_ac5_backoff_is_capped(tmp_path):
    """AC5 - backoff base 2s, factor 2, cap 30s."""
    rate_limited = Exception("HTTP Error 429")
    downloader = FakeDownloader(errors=[rate_limited] * 10)
    service, _, _, sleeps = make_service(tmp_path, downloader=downloader, max_retries=6)

    with pytest.raises(IngestionRateLimitedError):
        service.fetch(SHORTS_URL)

    assert sleeps == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


@pytest.mark.parametrize(
    "message", ["Private video", "Video unavailable", "Sign in to confirm"]
)
def test_ac5_unavailable_errors_are_never_retried(tmp_path, message):
    """AC5 - private/unavailable errors are never retried."""
    downloader = FakeDownloader(errors=[Exception(message)] * 5)
    service, _, _, sleeps = make_service(tmp_path, downloader=downloader)

    with pytest.raises(MediaUnavailableError):
        service.fetch(SHORTS_URL)

    assert len(downloader.urls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("platform_name", "hint"),
    [
        ("win32", "winget install Gyan.FFmpeg"),
        ("darwin", "brew install ffmpeg"),
        ("linux", "sudo apt update && sudo apt install -y ffmpeg"),
    ],
)
def test_ac6_missing_ffmpeg_fails_before_download(
    tmp_path, monkeypatch, platform_name, hint
):
    """AC6 - FFmpeg is not installed: fail BEFORE any download with an install hint."""
    monkeypatch.setattr("src.ingestion.sys.platform", platform_name)
    service, downloader, run, _ = make_service(tmp_path, which=lambda name: None)

    with pytest.raises(FFmpegNotFoundError, match=hint):
        service.fetch(SHORTS_URL)

    assert downloader.urls == []
    assert run.calls == []


def test_ac6_missing_ffprobe_is_also_a_preflight_failure(tmp_path):
    """AC6 / gap comment - ffprobe ships with FFmpeg and is required too."""
    service, downloader, _, _ = make_service(
        tmp_path, which=lambda name: None if name == "ffprobe" else f"/bin/{name}"
    )

    with pytest.raises(FFmpegNotFoundError, match="ffprobe"):
        service.fetch(SHORTS_URL)

    assert downloader.urls == []


def test_ac6_ffmpeg_failure_raises_and_cleans_up(tmp_path):
    """AC6 - video without an audio track / FFmpeg exits non-zero."""
    run = FakeRun(
        ffmpeg_returncode=1,
        ffmpeg_stderr="banner\nStream map '0:a' matches no streams.\n",
    )
    service, _, _, _ = make_service(tmp_path, run=run)

    with pytest.raises(
        AudioExtractionError, match="Stream map '0:a' matches no streams"
    ):
        service.fetch(SHORTS_URL)

    assert list(tmp_path.glob("raw_*")) == []


def test_ac6_audio_must_really_be_mp3(tmp_path):
    """AC6 - raw_audio.mp3 reports codec mp3 (ffprobe)."""
    service, _, _, _ = make_service(tmp_path, run=FakeRun(audio_codec="aac"))

    with pytest.raises(AudioExtractionError, match="codec 'aac'"):
        service.fetch(SHORTS_URL)

    assert list(tmp_path.glob("raw_*")) == []


@pytest.mark.parametrize(
    ("audio_duration", "ok"), [(57.5, True), (59.4, True), (30.0, False), (61.0, False)]
)
def test_ac6_audio_duration_within_one_second_of_video(tmp_path, audio_duration, ok):
    """AC6 - audio duration within 1s of the video."""
    service, _, _, _ = make_service(
        tmp_path, run=FakeRun(audio_duration=audio_duration)
    )

    if ok:
        service.fetch(SHORTS_URL)
    else:
        with pytest.raises(AudioExtractionError, match="truncated"):
            service.fetch(SHORTS_URL)


def test_ac7_stale_artifacts_removed_before_download(tmp_path):
    """AC7 - artifacts from a previous run are deleted before the download starts."""
    (tmp_path / "raw_video.mp4").write_bytes(b"old video")
    (tmp_path / "raw_audio.mp3").write_bytes(b"old audio")
    (tmp_path / "raw_video.mp4.part").write_bytes(b"partial")
    service, downloader, _, _ = make_service(tmp_path)

    service.fetch(REEL_URL)

    assert downloader.stale_present_at_start == [False]
    assert (tmp_path / "raw_video.mp4").read_bytes() == b"fake video"
    assert not (tmp_path / "raw_video.mp4.part").exists()


def test_ac7_failure_leaves_no_stale_files_behind(tmp_path):
    """AC7 - failure after stale files existed must not return old artifacts."""
    (tmp_path / "raw_video.mp4").write_bytes(b"old video")
    (tmp_path / "raw_audio.mp3").write_bytes(b"old audio")
    downloader = FakeDownloader(errors=[Exception("Private video")])
    service, _, _, _ = make_service(tmp_path, downloader=downloader)

    with pytest.raises(MediaUnavailableError):
        service.fetch(SHORTS_URL)

    assert list(tmp_path.glob("raw_*")) == []


def test_ac7_success_requires_non_empty_outputs(tmp_path):
    """AC7 - on success both files exist with size > 0; an empty MP3 is a failure."""
    service, _, _, _ = make_service(tmp_path, run=FakeRun(audio_bytes=b""))

    with pytest.raises(IngestionError, match="raw_audio.mp3"):
        service.fetch(SHORTS_URL)

    assert list(tmp_path.glob("raw_*")) == []

    service, _, _, _ = make_service(tmp_path)
    service.fetch(SHORTS_URL)
    assert (tmp_path / "raw_video.mp4").stat().st_size > 0
    assert (tmp_path / "raw_audio.mp3").stat().st_size > 0


def test_ac8_result_contract(tmp_path, monkeypatch):
    """AC8 - typed, frozen IngestionResult with the documented paths."""
    monkeypatch.chdir(tmp_path)
    downloader = FakeDownloader(duration=58.4567)
    service = MediaIngestionService(
        downloader_factory=downloader,
        run=FakeRun(video_duration=58.4567, audio_duration=58.4567),
        which=lambda name: f"/bin/{name}",
        sleep=lambda _: None,
    )

    result = service.fetch(SHORTS_URL)

    assert result.video_path == Path("temp/raw_video.mp4")
    assert result.audio_path == Path("temp/raw_audio.mp3")
    assert result.duration_s == 58.46
    assert isinstance(result.duration_s, float)
    assert isinstance(result.platform, Platform)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.duration_s = 1.0  # type: ignore[misc]


def test_ac8_duration_falls_back_to_ffprobe_when_downloader_has_none(tmp_path):
    """AC8 / gap comment - yt-dlp may return no duration; probe the video instead."""
    service, _, run, _ = make_service(
        tmp_path,
        downloader=FakeDownloader(duration=None),
        run=FakeRun(video_duration=12.34, audio_duration=12.34),
    )

    result = service.fetch(REEL_URL)

    assert result.duration_s == 12.34
    assert any(call[0] == "ffprobe" and call[-1].endswith(".mp4") for call in run.calls)

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, Self, TypeIs

import groq
from groq import Groq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.config import settings
from src.retry import RetryDecision, retry_with_backoff

logger = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".flac", ".ogg"})
_EXCERPT_CHARS = 500


class TranscriptionError(Exception):
    pass


class InvalidAudioError(TranscriptionError):
    pass


class TranscriptionProviderError(TranscriptionError):
    pass


class TranscriptionRateLimitError(TranscriptionError):
    pass


class TranscriptionContractError(TranscriptionError):
    pass


class NoSpeechDetectedError(TranscriptionError):
    pass


class TranscriptChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)

    @field_validator("start", "end")
    @classmethod
    def _round_2dp(cls, value: float) -> float:
        return round(value, 2)

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be < end")
        return self


class TranscriptionResult(BaseModel):
    chunks: list[TranscriptChunk]
    duration_s: float | None = None

    def to_json_array(self) -> list[dict[str, float | str]]:
        return [
            {"start": chunk.start, "end": chunk.end, "text": chunk.text}
            for chunk in self.chunks
        ]


class SpeechTranscriber(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...


def _is_number(value: object) -> TypeIs[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _retry_after_s(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    raw = response.headers.get("retry-after") if response is not None else None
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _decide(exc: Exception) -> RetryDecision:
    if isinstance(exc, groq.RateLimitError):
        return RetryDecision(True, _retry_after_s(exc))
    if isinstance(exc, groq.APIConnectionError):  # includes APITimeoutError
        return RetryDecision(True)
    if isinstance(exc, groq.APIStatusError) and exc.status_code >= 500:
        return RetryDecision(True, _retry_after_s(exc))
    return RetryDecision(False)


class GroqWhisperTranscriber:
    def __init__(
        self,
        client: Groq | None = None,
        *,
        model: str | None = None,
        max_retries: int | None = None,
        max_upload_bytes: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self.model = model or settings.whisper_model
        self.max_retries = (
            settings.transcribe_max_retries if max_retries is None else max_retries
        )
        self.max_upload_bytes = (
            settings.transcribe_max_upload_mb * 1024 * 1024
            if max_upload_bytes is None
            else max_upload_bytes
        )
        self._sleep = sleep

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        client = self._get_client()
        audio_path = Path(audio_path)
        self._validate_audio(audio_path)

        response = self._call_with_retry(client, audio_path)
        return self._to_result(response)

    def _get_client(self) -> Groq:
        if self._client is not None:
            return self._client
        if not settings.groq_api_key:
            raise TranscriptionProviderError(
                "GROQ_API_KEY is not set. Next step: add it to your .env file "
                "(free key at https://console.groq.com)."
            )
        # max_retries=0: our bounded backoff is the only retry budget.
        self._client = Groq(
            api_key=settings.groq_api_key,
            timeout=settings.transcribe_timeout_s,
            max_retries=0,
        )
        return self._client

    def _validate_audio(self, path: Path) -> None:
        if not path.is_file():
            raise InvalidAudioError(
                f"Audio file not found: {path}. Next step: run ingestion first."
            )
        if path.suffix.lower() not in _ALLOWED_EXTENSIONS:
            raise InvalidAudioError(
                f"Unsupported audio format {path.suffix!r}. Next step: convert to MP3."
            )
        size = path.stat().st_size
        if size == 0:
            raise InvalidAudioError(
                f"Audio file is empty: {path}. "
                "Next step: re-run ingestion; audio extraction failed."
            )
        if size > self.max_upload_bytes:
            raise InvalidAudioError(
                f"Audio file is {size / 1024 / 1024:.1f} MB, above the "
                f"{self.max_upload_bytes / 1024 / 1024:.0f} MB upload limit. "
                "Next step: use a shorter clip."
            )

    def _call_with_retry(self, client: Groq, audio_path: Path) -> Any:
        def operation() -> Any:
            with audio_path.open("rb") as handle:
                return client.audio.transcriptions.create(
                    model=self.model,
                    file=(audio_path.name, handle),
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                    temperature=0.0,
                )

        try:
            return retry_with_backoff(
                operation,
                decide=_decide,
                max_retries=self.max_retries,
                sleep=self._sleep,
            )
        except groq.RateLimitError as exc:
            raise TranscriptionRateLimitError(
                "Groq rate limit reached after "
                f"{self.max_retries} retries. Next step: wait and retry; the "
                "free-tier audio quota may be exhausted."
            ) from exc
        except (groq.AuthenticationError, groq.PermissionDeniedError) as exc:
            raise TranscriptionProviderError(
                "Groq rejected the credentials. Next step: check GROQ_API_KEY "
                "in your .env file."
            ) from exc
        except groq.APIError as exc:
            status = getattr(exc, "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise TranscriptionProviderError(
                f"Groq transcription request failed ({detail}) after "
                f"{self.max_retries} retries: {exc!s}"
            ) from exc

    def _to_result(self, response: Any) -> TranscriptionResult:
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        if not isinstance(payload, dict):
            raise TranscriptionContractError(
                f"Provider response is not an object: {str(payload)[:_EXCERPT_CHARS]}"
            )

        chunks = self._to_chunks(payload)
        if not chunks:
            raise NoSpeechDetectedError(
                "No speech found. Next step: check the video has spoken audio."
            )

        duration = payload.get("duration")
        if _is_number(duration) and duration > 0:
            duration_s = round(float(duration), 2)
        else:
            duration_s = max(chunk.end for chunk in chunks)
            logger.warning(
                "Provider response has no duration; using last chunk end (%.2fs)",
                duration_s,
            )
        return TranscriptionResult(chunks=chunks, duration_s=duration_s)

    def _to_chunks(self, payload: dict[str, Any]) -> list[TranscriptChunk]:
        segments = payload.get("segments")
        if not isinstance(segments, list):
            raise TranscriptionContractError(
                "Provider response is missing 'segments' (segments required)."
            )

        chunks: list[TranscriptChunk] = []
        previous_start: float | None = None
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise TranscriptionContractError(
                    f"Segment {index} is not an object (float required)."
                )
            start, end, text = (
                segment.get("start"),
                segment.get("end"),
                segment.get("text"),
            )
            if not _is_number(start) or not _is_number(end):
                raise TranscriptionContractError(
                    f"Segment {index} has non-numeric timestamps (float required): "
                    f"start={start!r}, end={end!r}"
                )
            if not isinstance(text, str):
                raise TranscriptionContractError(
                    f"Segment {index} has no text string (text required)."
                )
            if start < 0:
                raise TranscriptionContractError(
                    f"Segment {index} has negative start {start} (start >= 0)."
                )
            if end < start:
                raise TranscriptionContractError(
                    f"Segment {index} ends before it starts: {start} -> {end} "
                    "(start < end)."
                )
            if previous_start is not None and start < previous_start:
                raise TranscriptionContractError(
                    f"Segment {index} starts at {start}, before the previous "
                    f"segment at {previous_start} (non-decreasing start)."
                )
            previous_start = start

            rounded_start, rounded_end = round(start, 2), round(end, 2)
            if rounded_start >= rounded_end or not text.strip():
                logger.debug("Dropping empty or zero-length segment %d", index)
                continue
            chunks.append(
                TranscriptChunk(start=rounded_start, end=rounded_end, text=text)
            )
        return chunks

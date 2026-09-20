import logging
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from groq.types.audio import Transcription

from src import transcriber
from src.config import settings
from src.transcriber import (
    GroqWhisperTranscriber,
    InvalidAudioError,
    NoSpeechDetectedError,
    TranscriptChunk,
    TranscriptionContractError,
    TranscriptionProviderError,
    TranscriptionRateLimitError,
)


class FakeGroq:
    """Duck-typed stand-in for groq.Groq recording every transcription call."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")


def status_error(cls: type[groq.APIStatusError], status: int, **headers: str):
    response = httpx.Response(status, headers=headers, request=_request())
    return cls("provider said no", response=response, body=None)


def rate_limited(**headers: str) -> groq.RateLimitError:
    return status_error(groq.RateLimitError, 429, **headers)


def ok_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": "hello world",
        "duration": 58.4,
        "segments": [
            {
                "id": 0,
                "seek": 0,
                "start": 0.0,
                "end": 4.1,
                "text": " hello ",
                "tokens": [1],
            },
            {
                "id": 1,
                "seek": 0,
                "start": 4.1,
                "end": 9.5,
                "text": "world",
                "avg_logprob": -0.1,
            },
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "raw_audio.mp3"
    path.write_bytes(b"ID3 fake mp3 bytes")
    return path


def make(responses: list[Any], **kwargs: Any):
    client = FakeGroq(responses)
    sleeps: list[float] = []
    service = GroqWhisperTranscriber(client, sleep=sleeps.append, **kwargs)
    return service, client, sleeps


def test_ac1_happy_path_single_request_in_order_and_handle_closed(audio):
    """AC1 - Happy path."""
    service, client, sleeps = make([ok_payload()])

    result = service.transcribe(audio)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "whisper-large-v3"
    assert call["response_format"] == "verbose_json"
    assert call["timestamp_granularities"] == ["segment"]
    assert call["temperature"] == 0.0
    filename, handle = call["file"]
    assert filename == "raw_audio.mp3"
    assert handle.closed
    assert [c.start for c in result.chunks] == [0.0, 4.1]
    assert sleeps == []


@pytest.mark.parametrize(
    ("raw_start", "raw_end", "raw_text", "start", "end", "text"),
    [
        (12.3456, 20.1, " hello ", 12.35, 20.1, "hello"),
        (0.0, 3.999, "hi", 0.0, 4.0, "hi"),
        (59.994, 60.001, "bye", 59.99, 60.0, "bye"),
    ],
)
def test_ac2_boundary_normalisation(
    audio, raw_start, raw_end, raw_text, start, end, text
):
    """AC2 - Output schema and two-decimal precision (Scenario Outline rows)."""
    segment = {"start": raw_start, "end": raw_end, "text": raw_text}
    service, _, _ = make([ok_payload(segments=[segment])])

    result = service.transcribe(audio)

    assert result.to_json_array() == [{"start": start, "end": end, "text": text}]
    assert isinstance(result.chunks[0].start, float)
    assert isinstance(result.chunks[0].end, float)


def test_ac2_provider_only_fields_are_never_included(audio):
    """AC2 - tokens / avg_logprob / seek are not leaked."""
    service, _, _ = make([ok_payload()])

    result = service.transcribe(audio)

    for item in result.to_json_array():
        assert set(item) == {"start", "end", "text"}


def test_ac2_zero_length_and_blank_segments_are_dropped(audio):
    """AC2 - rounded start >= end, or blank text, is dropped."""
    segments = [
        {"start": 1.0, "end": 2.0, "text": "keep"},
        {"start": 10.001, "end": 10.004, "text": "rounds to zero length"},
        {"start": 11.0, "end": 12.0, "text": "   "},
        {"start": 13.0, "end": 14.0, "text": "also keep"},
    ]
    service, _, _ = make([ok_payload(segments=segments)])

    result = service.transcribe(audio)

    assert [c.text for c in result.chunks] == ["keep", "also keep"]


def test_ac2_transcript_chunk_model_enforces_the_contract():
    """AC2 - the model itself rejects extras, disorder and bad types."""
    with pytest.raises(ValueError):
        TranscriptChunk(start=5.0, end=5.0, text="x")
    with pytest.raises(ValueError):
        TranscriptChunk(start=-1.0, end=5.0, text="x")
    with pytest.raises(ValueError):
        TranscriptChunk.model_validate({"start": 1, "end": 2, "text": "x", "extra": 1})
    chunk = TranscriptChunk(start=1.2345, end=2.3456, text="  x ")
    assert (chunk.start, chunk.end, chunk.text) == (1.23, 2.35, "x")


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        ({"text": "no segments key"}, "segments required"),
        ({"segments": "nope"}, "segments required"),
        ({"segments": [{"start": -1.0, "end": 2.0, "text": "a"}]}, "start >= 0"),
        ({"segments": [{"start": 5.0, "end": 2.0, "text": "a"}]}, "start < end"),
        ({"segments": [{"start": "x", "end": 2.0, "text": "a"}]}, "float required"),
        ({"segments": [{"start": 1.0, "end": None, "text": "a"}]}, "float required"),
        ({"segments": ["not an object"]}, "float required"),
        ({"segments": [{"start": 1.0, "end": 2.0}]}, "text required"),
        (
            {
                "segments": [
                    {"start": 12.0, "end": 13.0, "text": "a"},
                    {"start": 9.0, "end": 10.0, "text": "b"},
                ]
            },
            "non-decreasing start",
        ),
    ],
)
def test_ac3_malformed_provider_response(audio, payload, rule):
    """AC3 - Provider response contract validation."""
    service, _, _ = make([payload])

    with pytest.raises(TranscriptionContractError, match=rule):
        service.transcribe(audio)


def test_ac3_non_object_response_is_a_contract_error(audio):
    """AC3 - a response that is not an object at all."""
    service, _, _ = make(["<html>gateway error</html>"])

    with pytest.raises(TranscriptionContractError, match="not an object"):
        service.transcribe(audio)


def test_ac3_real_sdk_model_extras_are_readable(audio):
    """Gap comment 1 - groq's Transcription types only `text`; extras must survive."""
    response = Transcription.model_validate(ok_payload())
    service, _, _ = make([response])

    result = service.transcribe(audio)

    assert len(result.chunks) == 2
    assert result.duration_s == 58.4


def test_ac4_rate_limit_retried_with_exponential_backoff_then_succeeds(audio):
    """AC4 - 429 twice, third call succeeds; backoff 1s/2s +/-20% jitter."""
    service, client, sleeps = make([rate_limited(), rate_limited(), ok_payload()])

    result = service.transcribe(audio)

    assert len(result.chunks) == 2
    assert len(client.calls) == 3
    assert 0.8 <= sleeps[0] <= 1.2
    assert 1.6 <= sleeps[1] <= 2.4


def test_ac4_retry_after_header_is_honoured(audio):
    """AC4 - Retry-After wins over the computed backoff."""
    service, _, sleeps = make([rate_limited(**{"retry-after": "7"}), ok_payload()])

    service.transcribe(audio)

    assert sleeps == [7.0]


@pytest.mark.parametrize(
    "transient",
    [
        status_error(groq.InternalServerError, 503),
        groq.APITimeoutError(request=_request()),
        groq.APIConnectionError(request=_request()),
    ],
)
def test_ac4_5xx_timeout_and_connection_errors_are_retried(audio, transient):
    """AC4 - 5xx / timeout / connection error share the retry budget."""
    service, client, _ = make([transient, ok_payload()])

    service.transcribe(audio)

    assert len(client.calls) == 2


def test_ac4_retries_never_exceed_configured_maximum(audio):
    """AC4 - settings.transcribe_max_retries (default 3) bounds the attempts."""
    service, client, _ = make([rate_limited()])
    assert service.max_retries == settings.transcribe_max_retries == 3

    with pytest.raises(TranscriptionRateLimitError):
        service.transcribe(audio)

    assert len(client.calls) == 4  # first attempt + 3 retries


def test_ac5_rate_limit_exhaustion_is_actionable(audio):
    """AC5 - retries exhausted."""
    service, client, sleeps = make([rate_limited()], max_retries=2)

    with pytest.raises(TranscriptionRateLimitError, match="wait and retry") as caught:
        service.transcribe(audio)

    assert len(client.calls) == 3
    assert len(sleeps) == 2
    assert isinstance(caught.value.__cause__, groq.RateLimitError)


@pytest.mark.parametrize(
    "error",
    [
        status_error(groq.AuthenticationError, 401),
        status_error(groq.PermissionDeniedError, 403),
    ],
)
def test_ac5_auth_failure_is_not_retried(audio, error):
    """AC5 - 401/403 raise TranscriptionProviderError immediately."""
    service, client, sleeps = make([error])

    with pytest.raises(TranscriptionProviderError, match="GROQ_API_KEY"):
        service.transcribe(audio)

    assert len(client.calls) == 1
    assert sleeps == []


def test_ac5_persistent_5xx_becomes_provider_error(audio):
    """AC5 - non-rate-limit failures after retries -> TranscriptionProviderError."""
    service, client, _ = make(
        [status_error(groq.InternalServerError, 500)], max_retries=1
    )

    with pytest.raises(TranscriptionProviderError, match="HTTP 500"):
        service.transcribe(audio)

    assert len(client.calls) == 2


def test_ac5_empty_api_key_fails_before_the_audio_file_is_read(tmp_path, monkeypatch):
    """AC5 - empty key: error is raised BEFORE the (here non-existent) file is touched."""
    monkeypatch.setattr(settings, "groq_api_key", "")
    service = GroqWhisperTranscriber(sleep=lambda _: None)

    with pytest.raises(TranscriptionProviderError, match="GROQ_API_KEY"):
        service.transcribe(tmp_path / "does_not_exist.mp3")


def test_ac5_key_value_is_never_printed_and_sdk_retries_are_disabled(
    audio, monkeypatch
):
    """AC5 + gap comment 1 - secret never leaks; client built with max_retries=0."""
    secret = "gsk_super_secret_value"
    monkeypatch.setattr(settings, "groq_api_key", secret)
    built: dict[str, Any] = {}

    def fake_groq_factory(**kwargs: Any) -> FakeGroq:
        built.update(kwargs)
        return FakeGroq([status_error(groq.AuthenticationError, 401)])

    monkeypatch.setattr(transcriber, "Groq", fake_groq_factory)
    service = GroqWhisperTranscriber(sleep=lambda _: None)

    with pytest.raises(TranscriptionProviderError) as caught:
        service.transcribe(audio)

    assert secret not in str(caught.value)
    assert built["api_key"] == secret
    assert built["max_retries"] == 0
    assert built["timeout"] == settings.transcribe_timeout_s


@pytest.mark.parametrize(
    ("condition", "next_step"),
    [
        ("missing", "run ingestion first"),
        ("empty", "re-run ingestion; audio extraction failed"),
        ("bad_extension", "convert to MP3"),
        ("too_large", "use a shorter clip"),
    ],
)
def test_ac6_invalid_audio_fails_fast_without_network(tmp_path, condition, next_step):
    """AC6 - Malformed or unusable audio input fails fast (no network call)."""
    path = tmp_path / "raw_audio.mp3"
    kwargs: dict[str, Any] = {}
    if condition == "empty":
        path.write_bytes(b"")
    elif condition == "bad_extension":
        path = tmp_path / "notes.txt"
        path.write_bytes(b"not audio")
    elif condition == "too_large":
        path.write_bytes(b"x" * 20)
        kwargs["max_upload_bytes"] = 10
    service, client, _ = make([ok_payload()], **kwargs)

    with pytest.raises(InvalidAudioError, match=next_step):
        service.transcribe(path)

    assert client.calls == []


def test_ac6_default_upload_limit_is_25_mb():
    """AC6 - transcribe_max_upload_mb defaults to 25."""
    service, _, _ = make([ok_payload()])

    assert service.max_upload_bytes == 25 * 1024 * 1024


@pytest.mark.parametrize(
    "segments",
    [[], [{"start": 0.0, "end": 2.0, "text": "   "}]],
    ids=["zero-segments", "whitespace-only"],
)
def test_ac7_no_speech_detected(audio, segments):
    """AC7 - never silently return an empty list."""
    service, _, _ = make([ok_payload(segments=segments)])

    with pytest.raises(NoSpeechDetectedError, match="spoken audio"):
        service.transcribe(audio)


def test_ac8_duration_is_exposed_to_two_decimals(audio):
    """AC8 - result.duration_s comes from the provider."""
    service, _, _ = make([ok_payload(duration=58.456)])

    result = service.transcribe(audio)

    assert result.duration_s == 58.46


def test_ac8_duration_falls_back_to_last_chunk_end_with_warning(audio, caplog):
    """AC8 - missing duration -> largest chunk end + WARNING; JSON array unchanged."""
    payload = ok_payload()
    del payload["duration"]
    service, _, _ = make([payload])

    with caplog.at_level(logging.WARNING, logger="src.transcriber"):
        result = service.transcribe(audio)

    assert result.duration_s == 9.5
    assert any("no duration" in record.getMessage() for record in caplog.records)
    assert result.to_json_array() == [
        {"start": 0.0, "end": 4.1, "text": "hello"},
        {"start": 4.1, "end": 9.5, "text": "world"},
    ]

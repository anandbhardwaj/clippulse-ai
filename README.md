# ClipPulse AI 🍸⚡

ClipPulse AI extracts semantic video segments from YouTube and Instagram URLs. It transcribes audio via Whisper Large v3, finds requested topics via LMM reasoning, and generates a bounded, looping HTML5 preview player.

---

## 🛦️ System Prerequisites

Ensure FFmpeg is installed on your operating system:
- *macOS:* `brew install ffmpegb
- *Ubuntu/Debian:* `sudo apt update && sudo apt install -y ffmpeg`
- *Windows:* `winget install Gyan.FFmpeg`

---

## 🚀 Quick Setup

1. Synchronize environment:
```bash
uv sync
```

�2. Configure credentials:
```bash
cp .env.example .env
```

Add your keys inside `.env`:
- `GROQ_API_KEY` (Free via https://console.groq.com)
- `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`

---

## 🟐 How to Run

```bash
uv run python main.py --url "<VIDEO_ULL>" --query "<SEARCH_PROMPT>"
```

### Example
```bash
uv run python main.py --url "https://www.youtube.com/shorts/sample" --query "Find*��number six"
```

Output player artifact is saved to `output/index.html`.

### Supported URLs

Only these forms are accepted (anything else fails fast with an actionable error):

- YouTube Shorts: `https://www.youtube.com/shorts/<id>`
- Instagram Reels: `https://www.instagram.com/reel/<id>/` (also `/reels/<id>/` and `/<user>/reel/<id>/`)

Private or login-gated media needs a logged-in session: export cookies in Netscape format and
set `YTDLP_COOKIES_FILE` in `.env`. FFmpeg and `ffprobe` must be on your `PATH`.

### Transcription

Audio is transcribed with Groq `whisper-large-v3` (requires `GROQ_API_KEY`) into chunks of
`{"start": float, "end": float, "text": str}` with two-decimal timestamps. Audio files above
25 MB are rejected before any request is made (use a shorter clip); rate limits are retried
with exponential backoff (3 retries by default) before failing with an actionable error.

### Finding the moment (LLM)

The transcript and your query go to the LLM chosen by `LLM_PROVIDER` (`anthropic` by default,
or `gemini`); set `LLM_MODEL` to override the default model. The reply is validated as
`{"start": float, "end": float, "summary": str}` with `0 <= start < end <= video duration`.
A malformed reply gets exactly one automatic repair attempt. The model always returns its
closest match, so treat results for topics that are not in the video with caution.

---

## 🟪 Verification & Tests

```bash
uv run pytest tests/
uv run ruff check .
```

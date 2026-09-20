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

---

## 🟪 Verification & Tests

```bash
uv run pytest tests/
uv run ruff check .
```

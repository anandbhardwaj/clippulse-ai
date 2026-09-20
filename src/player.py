import html
import json
import math
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from src.config import settings

if TYPE_CHECKING:
    from src.reasoner import TemporalSegment

_VIDEO_NAME = "clip.mp4"
_HTML_NAME = "index.html"

_PAGE = Template("""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src 'self' file:; style-src 'unsafe-inline'; script-src 'nonce-$nonce'">
<title>Clip preview</title>
<style>
:root { color-scheme: light dark; --bg: #f6f6f4; --fg: #1b1b1b; --muted: #5c5c5c; --card: #ffffff; --accent: #0b5fff; --danger: #b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg: #121212; --fg: #ececec; --muted: #a8a8a8; --card: #1e1e1e; --accent: #7aa2ff; --danger: #ff8a80; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 1rem; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 52rem; margin: 0 auto; }
h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
.range { color: var(--muted); margin: 0 0 1rem; font-variant-numeric: tabular-nums; }
.stage { position: relative; background: #000; border-radius: .5rem; overflow: hidden; }
video { display: block; width: 100%; max-height: 70vh; background: #000; }
.overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; background: rgba(0, 0, 0, .55); }
button { font: inherit; padding: .6rem 1.1rem; border-radius: .4rem; border: 0; background: var(--accent); color: #fff; cursor: pointer; }
button:focus-visible, video:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.toolbar { margin: .75rem 0; }
.summary { background: var(--card); padding: .9rem 1rem; border-radius: .5rem; margin: .75rem 0; overflow-wrap: anywhere; }
.note { color: var(--muted); }
.banner { color: var(--danger); font-weight: 600; }
[hidden] { display: none !important; }
</style>
</head>
<body data-state="loading" data-loops="0">
<main>
<h1>Clip preview</h1>
<p class="range">Segment $start_label s to $end_label s (looping)</p>
<div class="stage">
<video id="clip" src="$video_src" autoplay muted playsinline controls preload="auto" aria-label="Selected video segment"></video>
<div class="overlay" id="overlay" hidden><button type="button" id="play">Play segment</button></div>
</div>
<div class="toolbar"><button type="button" id="unmute" hidden>Unmute</button></div>
<p class="summary" id="summary">$summary</p>
<p class="note" id="note" hidden></p>
<p class="banner" id="banner" role="alert" hidden></p>
</main>
<script nonce="$nonce">
(function () {
  "use strict";
  var SEG = $segment_json;
  var TOLERANCE = 0.05;
  var video = document.getElementById("clip");
  var overlay = document.getElementById("overlay");
  var playBtn = document.getElementById("play");
  var unmuteBtn = document.getElementById("unmute");
  var note = document.getElementById("note");
  var banner = document.getElementById("banner");
  var effectiveEnd = SEG.end;
  var loops = 0;
  var scheduled = false;
  var failed = false;

  function setState(state) { document.body.setAttribute("data-state", state); }

  function fail(message) {
    failed = true;
    video.pause();
    banner.textContent = message;
    banner.hidden = false;
    overlay.hidden = true;
    setState("error");
  }

  function outside(t) { return t >= effectiveEnd || t < SEG.start - TOLERANCE; }

  function wrapAround(countLoop) {
    if (countLoop) {
      loops += 1;
      document.body.setAttribute("data-loops", String(loops));
    }
    video.currentTime = SEG.start;
    if (video.paused) { tryPlay(); }
  }

  function guard() {
    scheduled = false;
    if (failed) { return; }
    var t = video.currentTime;
    if (outside(t)) { wrapAround(t >= effectiveEnd); }
    schedule();
  }

  function schedule() {
    if (!scheduled && !failed && !video.paused && !video.ended) {
      scheduled = true;
      requestAnimationFrame(guard);
    }
  }

  function tryPlay() {
    var attempt = video.play();
    if (attempt && attempt.catch) {
      attempt.catch(function () {
        if (!failed) { overlay.hidden = false; setState("blocked"); }
      });
    }
  }

  function init() {
    var duration = video.duration;
    if (isFinite(duration)) {
      if (SEG.start >= duration) {
        fail("This segment starts after the end of the video.");
        return;
      }
      if (SEG.end > duration) {
        effectiveEnd = duration;
        note.textContent = "Segment trimmed to video length.";
        note.hidden = false;
      }
    }
    document.body.setAttribute("data-end", effectiveEnd.toFixed(2));
    if (outside(video.currentTime)) { video.currentTime = SEG.start; }
    unmuteBtn.hidden = !video.muted;
    tryPlay();
  }

  video.addEventListener("loadedmetadata", init);
  video.addEventListener("play", function () {
    if (failed) { return; }
    overlay.hidden = true;
    setState("playing");
    schedule();
  });
  video.addEventListener("seeking", function () {
    if (!failed && outside(video.currentTime)) { video.currentTime = SEG.start; }
  });
  // Browsers pause natively at the media-fragment end (and "ended" fires when the
  // segment reaches the last frame), so resume from the start in both cases.
  function atBoundary() { return video.currentTime >= effectiveEnd - TOLERANCE; }
  video.addEventListener("pause", function () {
    if (!failed && !video.seeking && atBoundary()) { wrapAround(true); }
  });
  video.addEventListener("ended", function () {
    if (!failed && atBoundary()) { wrapAround(true); }
  });
  video.addEventListener("volumechange", function () { unmuteBtn.hidden = !video.muted; });
  video.addEventListener("error", function () { fail("The video could not be loaded."); });
  playBtn.addEventListener("click", function () { video.muted = true; tryPlay(); });
  unmuteBtn.addEventListener("click", function () { video.muted = false; });
  if (video.readyState >= 1) { init(); }
})();
</script>
</body>
</html>
""")


class PlayerError(Exception):
    pass


class PlayerInputError(PlayerError):
    pass


class PlayerRenderError(PlayerError):
    pass


def _json_for_script(data: dict[str, float]) -> str:
    return (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class PreviewPlayerBuilder:
    def __init__(self, output_dir: Path | None = None):
        self.output_dir = Path(output_dir) if output_dir else settings.output_dir

    def build(self, video_path: Path, segment: TemporalSegment) -> Path:
        video_path = Path(video_path)
        start, end = self._validate(video_path, segment)

        page = _PAGE.substitute(
            nonce=secrets.token_urlsafe(16),
            video_src=f"{_VIDEO_NAME}#t={start:.2f},{end:.2f}",
            start_label=f"{start:.2f}",
            end_label=f"{end:.2f}",
            summary=html.escape(str(segment.summary), quote=True),
            segment_json=_json_for_script({"start": start, "end": end}),
        )

        video_target = self.output_dir / _VIDEO_NAME
        html_target = self.output_dir / _HTML_NAME
        temp_paths: list[Path] = []
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            video_tmp = self._temp_path(".mp4")
            temp_paths.append(video_tmp)
            shutil.copy2(video_path, video_tmp)

            html_tmp = self._temp_path(".html")
            temp_paths.append(html_tmp)
            html_tmp.write_text(page, encoding="utf-8")

            os.replace(video_tmp, video_target)
            os.replace(html_tmp, html_target)
        except OSError as exc:
            raise PlayerRenderError(
                f"Could not write the preview player to {self.output_dir}: {exc!s}"
            ) from exc
        finally:
            for leftover in temp_paths:
                leftover.unlink(missing_ok=True)
        return html_target

    def _temp_path(self, suffix: str) -> Path:
        handle, name = tempfile.mkstemp(
            dir=self.output_dir, prefix=".clippulse-", suffix=suffix
        )
        os.close(handle)
        return Path(name)

    @staticmethod
    def _validate(video_path: Path, segment: TemporalSegment) -> tuple[float, float]:
        if not video_path.is_file():
            raise PlayerInputError(
                f"Video file not found: {video_path}. Next step: run ingestion first."
            )
        start, end = segment.start, segment.end
        for name, value in (("start", start), ("end", end)):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise PlayerInputError(
                    f"Segment {name} must be a number, got {value!r}."
                )
            if not math.isfinite(value):
                raise PlayerInputError(f"Segment {name} must be finite, got {value!r}.")
        if start < 0:
            raise PlayerInputError(f"Segment start must be >= 0, got {start}.")
        if start >= end:
            raise PlayerInputError(
                f"Segment start must be < end, got start={start}, end={end}."
            )
        return float(start), float(end)

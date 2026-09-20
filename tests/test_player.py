import json
import math
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from src import player
from src.config import settings
from src.player import (
    PlayerInputError,
    PlayerRenderError,
    PreviewPlayerBuilder,
    _json_for_script,
)
from src.reasoner import TemporalSegment

VIDEO_BYTES = b"fake video bytes"
SEGMENT = TemporalSegment(start=12.35, end=20.10, summary="Explains item six")
HOSTILE = '</script><script>alert(1)</script> "quoted" & <b>bold</b>'


class Page(HTMLParser):
    """Parses the generated page: every tag with its attributes, plus the text
    that a browser would display inside <p id="summary">."""

    def __init__(self, page: str):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.summary = ""
        self._in_summary = False
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if attributes.get("id") == "summary":
            self._in_summary = True

    def handle_endtag(self, tag: str):
        if tag == "p":
            self._in_summary = False

    def handle_data(self, data: str):
        if self._in_summary:
            self.summary += data

    def find(self, tag: str) -> list[dict[str, str | None]]:
        return [attributes for name, attributes in self.tags if name == tag]


@pytest.fixture
def video(tmp_path: Path) -> Path:
    path = tmp_path / "raw_video.mp4"
    path.write_bytes(VIDEO_BYTES)
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    return tmp_path / "output"


def build(video: Path, out: Path, segment: TemporalSegment = SEGMENT) -> Path:
    return PreviewPlayerBuilder(out).build(video, segment)


def render(video: Path, out: Path, segment: TemporalSegment = SEGMENT) -> str:
    return build(video, out, segment).read_text(encoding="utf-8")


def snapshot(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def unvalidated(start: Any, end: Any) -> TemporalSegment:
    """A segment that skipped model validation, as a careless caller could build."""
    return TemporalSegment.model_construct(start=start, end=end, summary="s")


def test_ac1_bounded_autoplaying_player_is_generated(video, out):
    """AC1 - Happy path: bounded, autoplaying player is generated."""
    result = build(video, out)

    assert result == out / "index.html"
    assert (out / "clip.mp4").read_bytes() == VIDEO_BYTES
    assert video.read_bytes() == VIDEO_BYTES  # copied, not moved
    page = Page(result.read_text(encoding="utf-8"))
    (clip,) = page.find("video")
    assert clip["src"] == "clip.mp4#t=12.35,20.10"
    for attribute in ("autoplay", "muted", "playsinline", "controls"):
        assert attribute in clip
    assert page.summary == "Explains item six"


@pytest.mark.parametrize(
    ("start", "end", "fragment"),
    [
        (0.0, 5.5, "0.00,5.50"),
        (12.35, 20.1, "12.35,20.10"),
        (59.99, 60.0, "59.99,60.00"),
    ],
)
def test_ac2_media_fragment_formatting(video, out, start, end, fragment):
    """AC2 - Media Fragment formatting (Scenario Outline rows)."""
    segment = TemporalSegment(start=start, end=end, summary="s")

    (clip,) = Page(render(video, out, segment)).find("video")

    assert clip["src"] == f"clip.mp4#t={fragment}"


def test_ac3_native_loop_attribute_is_not_relied_on(video, out):
    """AC3 - the JavaScript guard, not the native loop attribute, bounds the loop.

    Only this clause is checkable without a browser; the looping and scrubbing
    behaviour itself is verified manually (see the story's Definition of Done).
    """
    (clip,) = Page(render(video, out)).find("video")

    assert "loop" not in clip


def test_ac5_page_is_self_contained_and_offline(video, out):
    """AC5 - Zero dependencies, fully offline."""
    text = render(video, out)
    page = Page(text)

    assert not re.search(r"https?://", text)
    assert "@import" not in text
    assert "url(" not in text
    assert page.find("link") == []
    assert all("src" not in attributes for attributes in page.find("script"))
    references = [
        attributes[name]
        for _, attributes in page.tags
        for name in ("src", "href")
        if name in attributes
    ]
    assert references == ["clip.mp4#t=12.35,20.10"]  # only the local clip


@pytest.mark.parametrize(
    ("start", "end", "problem"),
    [
        (20.0, 20.0, "start must be < end"),
        (30.0, 12.0, "start must be < end"),
        (-1.0, 10.0, "start must be >= 0"),
        (math.nan, 10.0, "must be finite"),
        (1.0, math.nan, "must be finite"),
        (math.inf, 10.0, "must be finite"),
        (1.0, math.inf, "must be finite"),
        ("1", 5.0, "must be a number"),
        (True, 5.0, "must be a number"),
    ],
)
def test_ac6_invalid_segment_is_rejected_before_any_write(
    video, out, start, end, problem
):
    """AC6 - invalid segment: PlayerInputError naming the problem, nothing written."""
    with pytest.raises(PlayerInputError, match=problem):
        build(video, out, unvalidated(start, end))

    assert not out.exists()


def test_ac6_missing_video_is_rejected_before_any_write(tmp_path, out):
    """AC6 - a video path that does not exist."""
    with pytest.raises(PlayerInputError, match="Video file not found"):
        build(tmp_path / "missing.mp4", out)

    assert not out.exists()


def test_ac6_rejected_build_does_not_modify_existing_output(video, out):
    """AC6 - no output file is created or modified when the input is rejected."""
    build(video, out)
    before = snapshot(out)

    with pytest.raises(PlayerInputError):
        build(video, out, unvalidated(5.0, 5.0))

    assert snapshot(out) == before


def test_ac6_hostile_summary_is_inert_visible_text(video, out):
    """AC6 - hostile summary text is HTML-escaped and never becomes markup."""
    segment = TemporalSegment(start=12.35, end=20.1, summary=HOSTILE)
    text = render(video, out, segment)
    page = Page(text)

    assert page.summary == HOSTILE  # displayed verbatim, as text
    assert len(page.find("script")) == 1  # only the page's own script exists
    assert page.find("b") == []
    assert "<script>alert(1)" not in text
    assert "&lt;/script&gt;&lt;script&gt;alert(1)" in text


def test_ac6_template_syntax_in_summary_is_not_interpreted(video, out):
    """AC6 - '$' sequences in LLM text are data, not template placeholders."""
    segment = TemporalSegment(start=1.0, end=2.0, summary="Costs $5, ${name} and $$")

    assert Page(render(video, out, segment)).summary == "Costs $5, ${name} and $$"


def test_ac6_script_payload_is_json_with_markup_characters_escaped(video, out):
    """AC6 - values embedded in JavaScript are serialised as JSON."""
    text = render(video, out)

    payload = re.search(r"var SEG = (.*?);", text)

    assert payload is not None
    assert json.loads(payload.group(1)) == {"start": 12.35, "end": 20.1}


def test_ac6_json_helper_escapes_angle_brackets_and_ampersands():
    """AC6 - '<' (and '>' and '&') in embedded values become unicode escapes."""
    encoded = _json_for_script({"</script>&": 1.0})

    assert not set("<>&") & set(encoded)
    assert "\\u003c" in encoded
    assert json.loads(encoded) == {"</script>&": 1.0}


def test_ac6_csp_blocks_external_loads_and_pins_the_inline_script(video, out):
    """AC6 - the page carries a Content-Security-Policy that blocks external loads."""
    page = Page(render(video, out))

    (meta,) = [
        attributes
        for attributes in page.find("meta")
        if attributes.get("http-equiv") == "Content-Security-Policy"
    ]
    (script,) = page.find("script")
    policy = meta["content"]
    assert policy is not None and "default-src 'none'" in policy
    assert script["nonce"]
    assert f"script-src 'nonce-{script['nonce']}'" in policy


def test_ac6_csp_nonce_is_fresh_for_every_render(video, tmp_path):
    """AC6 - the script nonce is generated per render, never reused."""
    nonces = {
        Page(render(video, tmp_path / f"out{i}")).find("script")[0]["nonce"]
        for i in range(3)
    }

    assert len(nonces) == 3


def test_ac8_output_directory_is_created_when_missing(video, tmp_path):
    """AC8 - output/ is created if needed."""
    out = tmp_path / "nested" / "output"

    assert build(video, out) == out / "index.html"
    assert (out / "clip.mp4").is_file()


def test_ac8_previous_build_is_replaced_and_no_temp_files_remain(video, out):
    """AC8 - old files are replaced; nothing but the two artifacts is left behind."""
    out.mkdir()
    (out / "index.html").write_text("old page")
    (out / "clip.mp4").write_bytes(b"old clip")

    build(video, out)

    assert (out / "clip.mp4").read_bytes() == VIDEO_BYTES
    assert "old page" not in (out / "index.html").read_text(encoding="utf-8")
    assert sorted(path.name for path in out.iterdir()) == ["clip.mp4", "index.html"]


def test_ac8_page_is_written_to_a_temp_file_then_moved_with_os_replace(
    video, out, monkeypatch
):
    """AC8 - index.html is never observable half-written."""
    real_replace = os.replace
    moves: list[tuple[Path, Path]] = []

    def spy(source, target):
        source, target = Path(source), Path(target)
        if target.name == "index.html":
            # by the time it is moved into place the temp file is complete
            assert source != target
            assert source.parent == target.parent
            assert source.read_text(encoding="utf-8").rstrip().endswith("</html>")
        moves.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(player.os, "replace", spy)

    build(video, out)

    assert {target.name for _, target in moves} == {"index.html", "clip.mp4"}


def test_ac8_video_path_with_spaces_and_non_ascii_characters(tmp_path, out):
    """AC8 - a video path containing spaces or non-ASCII characters works."""
    source = tmp_path / "mi vídeo ñ 日本.mp4"
    source.write_bytes(b"unicode bytes")
    segment = TemporalSegment(start=1.0, end=2.0, summary="Explicación: número seis ✓")

    result = build(source, out, segment)

    assert (out / "clip.mp4").read_bytes() == b"unicode bytes"
    page = Page(result.read_text(encoding="utf-8"))
    assert page.summary == "Explicación: número seis ✓"


def test_ac8_failed_copy_leaves_previous_output_untouched(video, out, monkeypatch):
    """AC8 - a failed build leaves the previous output untouched."""
    build(video, out)
    before = snapshot(out)

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(player.shutil, "copy2", disk_full)

    with pytest.raises(PlayerRenderError, match="disk full") as caught:
        build(video, out, TemporalSegment(start=1.0, end=2.0, summary="new"))

    assert isinstance(caught.value.__cause__, OSError)
    assert snapshot(out) == before  # old artifacts intact, no temp files left


def test_ac8_failed_swap_leaves_previous_output_untouched(video, out, monkeypatch):
    """AC8 - a failure while moving files into place changes nothing."""
    build(video, out)
    before = snapshot(out)

    def locked(*_args, **_kwargs):
        raise PermissionError("file is in use")

    monkeypatch.setattr(player.os, "replace", locked)

    with pytest.raises(PlayerRenderError, match="file is in use"):
        build(video, out, TemporalSegment(start=1.0, end=2.0, summary="new"))

    assert snapshot(out) == before


def test_ac8_unwritable_output_location_raises_render_error(video, out):
    """AC8 - an output path that cannot be a directory is a typed error."""
    out.write_text("I am a file, not a directory")

    with pytest.raises(PlayerRenderError, match="Could not write"):
        build(video, out)


def test_default_output_directory_comes_from_settings(video, tmp_path, monkeypatch):
    """Design - PreviewPlayerBuilder() defaults to settings.output_dir."""
    monkeypatch.setattr(settings, "output_dir", tmp_path / "configured")

    result = PreviewPlayerBuilder().build(video, SEGMENT)

    assert result == tmp_path / "configured" / "index.html"
    assert result.is_file()

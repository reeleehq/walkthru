"""The marker, the camera move and the clip encoder — on synthetic captures, no browser needed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

Image = pytest.importorskip("PIL.Image")

from walkthru.evidence import (  # noqa: E402
    Capture,
    apply_marker,
    iter_highlight_frames,
    render_highlight_clip,
)
from walkthru.evidence.marker import DARK_PAGE_AMBER, HIGHLIGHTER_YELLOW  # noqa: E402


def _is_yellow(px) -> bool:
    r, g, b = px[:3]
    return r > 200 and g > 180 and b < 140


def _yellow_count(img) -> int:
    b = img.convert("RGB").tobytes()
    return sum(1 for i in range(0, len(b), 3) if _is_yellow(b[i : i + 3]))


def test_marker_multiplies_yellow_on_light_pages():
    img = Image.new("RGB", (200, 60), "white")
    img.paste((0, 0, 0), (20, 28, 180, 32))  # a thin stroke: text is mostly paper
    out = apply_marker(img, [(20, 20, 180, 40)])
    assert out.getpixel((10, 30)) == (255, 255, 255), "outside the box: untouched"
    assert out.getpixel((100, 21)) == HIGHLIGHTER_YELLOW, "paper turns highlighter yellow"
    assert out.getpixel((100, 30)) == (0, 0, 0), "ink stays ink"
    assert img.getpixel((100, 21)) == (255, 255, 255), "the input is not modified"


def test_marker_on_dark_pages_is_an_amber_tint_and_underline():
    img = Image.new("RGB", (200, 60), (10, 10, 10))
    out = apply_marker(img, [(20, 20, 180, 40)])
    r, g, b = out.getpixel((100, 25))
    assert r > 80 and g > 60 and b < 20, "visible amber tint where multiply would vanish"
    assert out.getpixel((100, 41)) == DARK_PAGE_AMBER, "underline at the box's foot"


def test_marker_sweeps_in_reading_order():
    img = Image.new("RGB", (300, 100), "white")
    rects = [(0, 10, 100, 30), (0, 60, 100, 80)]
    half = apply_marker(img, rects, 0.5, pad=(0, 0))
    assert _is_yellow(half.getpixel((90, 20))) and not _is_yellow(half.getpixel((10, 70)))
    quarter = apply_marker(img, rects, 0.25, pad=(0, 0))
    assert _is_yellow(quarter.getpixel((40, 20))) and not _is_yellow(quarter.getpixel((60, 20)))
    assert apply_marker(img, rects, 0.0).getpixel((10, 20)) == (255, 255, 255)


def _capture(tmp_path, *, found=True) -> Capture:
    img = Image.new("RGB", (1600, 900), "white")
    rect = (500, 500, 1100, 530)
    img.paste((30, 30, 30), (500, 505, 1100, 525))
    png = tmp_path / "page.png"
    img.save(png)
    cap = Capture(
        png=png, size=img.size, rects=(rect,) if found else (), found=found,
        verified_by="dom" if found else None, reason=None if found else "occluded",
    )  # fmt: skip
    cap.save()
    return cap


def test_frames_move_toward_the_passage_and_sweep_in_time(tmp_path):
    cap = _capture(tmp_path)
    frames = list(iter_highlight_frames(cap, duration=2.0, sweep=(0.5, 1.5), fps=10, size=(320, 180)))
    assert len(frames) == 20 and all(f.size == (320, 180) for f in frames)
    assert _yellow_count(frames[0]) == 0 and _yellow_count(frames[4]) == 0, "nothing before the sweep"
    assert _yellow_count(frames[-1]) > 100, "the whole passage is marked at the end"

    progress = []

    def recording_marker(image, rects, p):
        progress.append(round(p, 3))
        return apply_marker(image, rects, p)

    list(iter_highlight_frames(
        cap, duration=2.0, sweep=(0.5, 1.5), fps=10, size=(320, 180), marker=recording_marker
    ))  # fmt: skip
    # Drawn only when progress changes: once per frame inside the sweep, linear in time.
    assert progress == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    # The camera ended closer: the passage spans more of the frame at the end than at the start.
    def dark_cols(f):
        return sum(1 for x in range(f.size[0]) if any(sum(f.getpixel((x, y))) < 300 for y in range(f.size[1])))
    assert dark_cols(frames[-1]) > dark_cols(frames[0]) * 1.4


def test_unverified_capture_is_never_highlighted(tmp_path):
    cap = _capture(tmp_path, found=False)
    frames = list(iter_highlight_frames(cap, duration=1.0, sweep=(0.1, 0.9), fps=10, size=(320, 180)))
    assert all(_yellow_count(f) == 0 for f in frames)


def test_capture_rejects_rects_without_verification(tmp_path):
    with pytest.raises(ValueError):
        Capture(png=tmp_path / "x.png", size=(10, 10), rects=((0, 0, 1, 1),), found=False)


def test_sweep_must_fit_the_clip(tmp_path):
    cap = _capture(tmp_path)
    with pytest.raises(ValueError):
        next(iter_highlight_frames(cap, duration=2.0, sweep=(1.5, 2.5)))


def test_render_uses_the_encoder_seam(tmp_path):
    cap = _capture(tmp_path)
    got = {}

    def encoder(argv, frames):
        got["argv"] = list(argv)
        got["n"] = sum(1 for _ in frames)

    out = render_highlight_clip(cap.sidecar, tmp_path / "c.mp4", duration=1.0, fps=12, size=(320, 180), encoder=encoder)
    assert out == tmp_path / "c.mp4" and got["n"] == 12
    assert "320x180" in got["argv"] and "libx264" in got["argv"] and "bt709" in got["argv"]


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_render_writes_a_playable_mp4(tmp_path):
    cap = _capture(tmp_path)
    out = render_highlight_clip(cap, tmp_path / "c.mp4", duration=1.0, sweep=(0.2, 0.8), fps=10, size=(320, 180))
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=width,height,nb_read_frames,pix_fmt,color_range", "-of", "json", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout)["streams"][0]  # fmt: skip
    assert (probe["width"], probe["height"]) == (320, 180)
    assert int(probe["nb_read_frames"]) == 10
    assert probe["pix_fmt"] == "yuv420p" and probe["color_range"] == "tv"


def test_importing_walkthru_pulls_in_no_evidence_vendor():
    code = (
        "import sys, walkthru\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('playwright', 'pymupdf', 'fitz', 'PIL', 'ocracy', 'rapidfuzz') "
        "or m.startswith('walkthru.evidence')]\n"
        "assert not bad, bad\n"
        "assert callable(walkthru.capture_passage)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)

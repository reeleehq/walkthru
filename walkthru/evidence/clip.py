"""Render a captured passage as a short clip: the camera eases toward it while a marker sweeps.

The clip opens on the whole capture, then eases (smoothstep) to a 16:9 window around the passage
— never narrower than ``min_window_frac`` of the image, so the page stays recognisable — and the
highlighter sweeps across the passage between ``sweep[0]`` and ``sweep[1]`` seconds. Time those to
the narration words that speak the quote and the pen moves with the voice.

A capture that was **not** verified (``found=False``) renders as a gentle push-in on the page with
no highlight: drawing a marker where the text was not confirmed is how a video ends up pointing at
the wrong sentence.

Frames are cropped with sub-pixel accuracy (an affine resample, not an integer crop), so a slow
move does not step a pixel at a time. :func:`iter_highlight_frames` is the pure frame source;
:func:`render_highlight_clip` pipes it to ffmpeg (H.264, ``yuv420p``, BT.709, TV range).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, Union

from walkthru.evidence.capture import Capture
from walkthru.evidence.marker import _pil, apply_marker

__all__ = [
    "iter_highlight_frames",
    "render_highlight_clip",
    "smoothstep",
    "target_window",
]

Window = tuple[float, float, float, float]  # (left, top, width, height)


def smoothstep(x: float) -> float:
    """Ease in and out on ``[0, 1]`` (clamped).

    >>> smoothstep(0.0), smoothstep(0.5), smoothstep(2.0)
    (0.0, 0.5, 1.0)
    """
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def target_window(
    W: float,
    H: float,
    rects: Sequence[Sequence[float]],
    *,
    aspect: float = 16 / 9,
    min_frac: float = 0.55,
    margin: float = 0.35,
) -> Window:
    """A window ``aspect`` wide around ``rects``, at least ``min_frac`` of ``W``, inside the image.

    >>> [round(v, 1) for v in target_window(1600, 900, [(700, 400, 900, 420)])]
    [360.0, 162.5, 880.0, 495.0]
    """
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    w = max(
        W * min_frac, (x1 - x0) * (1 + margin), (y1 - y0) * (1 + 2 * margin) * aspect
    )
    w = min(w, W)
    h = w / aspect
    if h > H:
        h, w = H, H * aspect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(0.0, cx - w / 2), W - w)
    top = min(max(0.0, cy - h / 2), H - h)
    return float(left), float(top), float(w), float(h)


def _as_capture(capture: Union[Capture, str, Path]) -> Capture:
    return capture if isinstance(capture, Capture) else Capture.load(capture)


def _fit_aspect(img: Any, rects: Sequence[Sequence[float]], aspect: float):
    """Pad ``img`` to ``aspect`` with its own corner colour; shift ``rects`` to match."""
    Image = _pil()[0]
    W, H = img.size
    if abs(W / H - aspect) <= 0.01:
        return img, [tuple(r) for r in rects]
    if W / H > aspect:
        nW, nH = W, int(round(W / aspect))
    else:
        nW, nH = int(round(H * aspect)), H
    ox, oy = (nW - W) // 2, (nH - H) // 2
    canvas = Image.new("RGB", (nW, nH), img.getpixel((0, 0)))
    canvas.paste(img, (ox, oy))
    return canvas, [(r[0] + ox, r[1] + oy, r[2] + ox, r[3] + oy) for r in rects]


def iter_highlight_frames(
    capture: Union[Capture, str, Path],
    *,
    duration: float,
    sweep: Optional[tuple[float, float]] = None,
    fps: int = 30,
    size: tuple[int, int] = (1920, 1080),
    move: Optional[float] = None,
    min_window_frac: float = 0.55,
    unverified_zoom: float = 1.08,
    marker: Callable[..., Any] = apply_marker,
) -> Iterator[Any]:
    """Yield the clip's frames as RGB PIL images of ``size``.

    Args:
        capture: A :class:`Capture` or the path of its JSON sidecar.
        duration: Clip length in seconds.
        sweep: ``(start, end)`` seconds of the highlighter sweep; default
            ``(0.25, 0.65) × duration``.
        fps: Frames per second.
        size: Output frame size; its aspect is the camera's.
        move: Seconds the camera takes to reach the passage; default ends shortly after the
            sweep starts.
        min_window_frac: The final window is at least this fraction of the image width.
        unverified_zoom: Push-in factor for a capture without a verified passage.
        marker: ``(image, rects, progress) -> image`` — the highlight drawn (seam).
    """
    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")
    start, end = sweep if sweep is not None else (0.25 * duration, 0.65 * duration)
    if not 0 <= start < end <= duration:
        raise ValueError(
            f"sweep must satisfy 0 <= start < end <= duration, got {(start, end)}"
        )
    cap = _as_capture(capture)
    Image = _pil()[0]
    aspect = size[0] / size[1]
    img, rects = _fit_aspect(
        Image.open(cap.png).convert("RGB"), cap.rects if cap.found else (), aspect
    )
    W, H = img.size
    full: Window = (0.0, 0.0, float(W), float(H))
    if rects:
        target = target_window(W, H, rects, aspect=aspect, min_frac=min_window_frac)
        move = move if move is not None else max(1.5, min(duration * 0.6, start + 0.8))
    else:
        z = unverified_zoom
        target = (W * (1 - 1 / z) / 2, H * (1 - 1 / z) / 2, W / z, H / z)
        move = move if move is not None else duration
    resample = (
        Image.Resampling.BICUBIC if hasattr(Image, "Resampling") else Image.BICUBIC
    )
    n = max(1, int(round(duration * fps)))
    marked, marked_at = img, 0.0
    for i in range(n):
        t = i / fps
        e = smoothstep(t / move) if move > 0 else 1.0
        left, top, w, h = (a + (b - a) * e for a, b in zip(full, target))
        progress = min(1.0, max(0.0, (t - start) / (end - start))) if rects else 0.0
        if progress != marked_at:
            marked, marked_at = marker(img, rects, progress), progress
        yield marked.transform(
            size, Image.AFFINE, (w / size[0], 0, left, 0, h / size[1], top), resample
        )


def _encode_argv(
    out: Path, *, size: tuple[int, int], fps: int, crf: int, ffmpeg: str
) -> list[str]:
    return [
        ffmpeg, "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(fps),
        "-i", "-",
        "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-color_range", "tv",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def _ffmpeg_encode(argv: Sequence[str], frames: Iterable[bytes]) -> None:
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            proc.stdin.write(frame)
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait()
    if proc.returncode:
        raise RuntimeError(
            f"ffmpeg failed encoding the highlight clip:\n{err.strip()[-800:]}"
        )


def render_highlight_clip(
    capture: Union[Capture, str, Path],
    out: Union[str, Path],
    *,
    duration: float,
    sweep: Optional[tuple[float, float]] = None,
    fps: int = 30,
    size: tuple[int, int] = (1920, 1080),
    crf: int = 18,
    ffmpeg: str = "ffmpeg",
    encoder: Optional[Callable[[Sequence[str], Iterable[bytes]], None]] = None,
    **frame_options: Any,
) -> Path:
    """Render ``capture`` as an mp4 at ``out``; returns ``out``.

    ``sweep=(start, end)`` is when, in seconds from the clip's start, the highlighter crosses the
    passage — set it to the times the narration speaks the quote. ``encoder`` is a seam
    ``(argv, frame_bytes) -> None`` (default: pipe to ffmpeg). Other keyword arguments go to
    :func:`iter_highlight_frames`.
    """
    if size[0] % 2 or size[1] % 2:
        raise ValueError(f"H.264 yuv420p needs an even frame size, got {size}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if encoder is None:
        from walkthru.adapters.gif.render_target import check_ffmpeg

        check_ffmpeg(ffmpeg=ffmpeg, ffprobe=ffmpeg.replace("ffmpeg", "ffprobe"))
        encoder = _ffmpeg_encode
    frames = iter_highlight_frames(
        capture, duration=duration, sweep=sweep, fps=fps, size=size, **frame_options
    )
    encoder(
        _encode_argv(out, size=size, fps=fps, crf=crf, ffmpeg=ffmpeg),
        (f.tobytes() for f in frames),
    )
    return out

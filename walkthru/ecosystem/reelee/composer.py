"""Poster composers: put stills of any aspect onto the film's frame before the Ken Burns pass.

A real still is rarely 16:9 (a 4.6:1 nav-bar crop, a 0.85:1 tall page). A
:data:`~walkthru.ecosystem.reelee.render_target.PosterComposer` maps ``(image_or_None, step)`` to
the image the film should use. :func:`letterbox_poster` is the stock one: crop (optional), fit
onto a ``size`` canvas, a chapter strip with the step's section title along the top, and a title
card (the section title or a beat's text) for a step with no poster. The text comes from the
Demo Document, which stays the single source of truth.

Needs Pillow (the ``evidence`` extra); imported lazily so importing the package does not.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union

from walkthru.core.timeline import ResolvedStep

Color = Union[str, tuple]
#: ``step -> text`` for the strip; ``None`` draws no strip.
StripText = Callable[[ResolvedStep], Optional[str]]

DEFAULT_SIZE = (1920, 1080)


def section_title(step: ResolvedStep) -> Optional[str]:
    """The default strip text: the title of the step's section."""
    return step.section_title


def _card_text(step: ResolvedStep) -> Optional[str]:
    return getattr(step.step, "text", None) or step.section_title


def _font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "Helvetica.ttc", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _centered_text(draw, box, text: str, *, fill, size: int) -> None:
    font = _font(size)
    x0, y0, x1, y1 = box
    while size > 8 and draw.textlength(text, font=font) > (x1 - x0) * 0.92:
        size -= 2
        font = _font(size)
    w = draw.textlength(text, font=font)
    draw.text(
        ((x0 + x1 - w) / 2, (y0 + y1) / 2), text, font=font, fill=fill, anchor="lm"
    )


def letterbox_poster(
    size: tuple = DEFAULT_SIZE,
    *,
    strip: Optional[StripText] = section_title,
    crop_top: Optional[int] = None,
    bg: Color = "#101418",
    strip_bg: Color = "#1d2733",
    fg: Color = "#f2f5f8",
    strip_height: float = 0.08,
    work_dir: Optional[Union[str, Path]] = None,
):
    """A composer that letterboxes each still onto a ``size`` frame, under an optional strip.

    Args:
        size: ``(width, height)`` of the film frame.
        strip: ``step -> text`` for the chapter strip (default: the section title); ``None`` or a
            ``None`` result draws no strip.
        crop_top: keep only the top ``crop_top`` pixels of a very tall still (``None``: keep all).
        bg, strip_bg, fg: frame, strip and text colours.
        strip_height: strip height as a fraction of the frame height.
        work_dir: where composed frames are written (default: a fresh temporary directory that
            lives as long as the process). Output names derive from the inputs, so a rerun reuses
            them.

    A step with no poster gets a title card (a beat's text, else the section title), or stays
    without an image when it has neither.
    """
    from PIL import Image, ImageDraw

    width, height = size
    out_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="walkthru_frames_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    strip_h = int(height * strip_height)

    def compose(image: Optional[Path], step: ResolvedStep) -> Optional[Path]:
        label = strip(step) if strip else None
        card = None
        if image is None:
            card = _card_text(step)
            if card is None:
                return None
        canvas = Image.new("RGB", size, bg)
        draw = ImageDraw.Draw(canvas)
        top = strip_h if label else 0
        if image is not None:
            with Image.open(image) as src:
                still = src.convert("RGB")
            if crop_top is not None and still.height > crop_top:
                still = still.crop((0, 0, still.width, crop_top))
            scale = min(width / still.width, (height - top) / still.height)
            fitted = still.resize(
                (max(1, round(still.width * scale)), max(1, round(still.height * scale)))
            )
            canvas.paste(fitted, ((width - fitted.width) // 2, top + (height - top - fitted.height) // 2))
        else:
            _centered_text(draw, (0, top, width, height), card, fill=fg, size=height // 10)
        if label:
            draw.rectangle((0, 0, width, strip_h), fill=strip_bg)
            _centered_text(draw, (0, 0, width, strip_h), label, fill=fg, size=strip_h // 2)
        key = hashlib.sha1(
            f"{image}|{step.step_id}|{label}|{card}|{size}|{crop_top}".encode()
        ).hexdigest()[:12]
        out = out_dir / f"{step.step_id}-{key}.png"
        canvas.save(out)
        return out

    return compose

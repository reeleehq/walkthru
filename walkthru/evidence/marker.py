"""A highlighter-pen marker over a passage's boxes, swept in reading order.

On a light page the marker is a **multiply** blend in highlighter yellow: dark text stays dark and
paper turns yellow, as with a real pen. Multiply is invisible on a dark theme (yellow times
near-black is near-black), so where the passage sits on a dark background (mean luminance below
``dark_threshold``) the marker becomes an amber tint plus an underline.

``progress`` in ``[0, 1]`` sweeps the marker across the boxes in order, as a fraction of their
total width, so a sweep timed to narration moves at reading pace across line breaks.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["HIGHLIGHTER_YELLOW", "DARK_PAGE_AMBER", "apply_marker"]

#: The multiply colour of a yellow highlighter (``(1.0, 0.93, 0.35)`` × 255).
HIGHLIGHTER_YELLOW = (255, 237, 89)
#: The tint and underline colour used on dark pages.
DARK_PAGE_AMBER = (255, 205, 0)


def _pil():
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageStat
    except ImportError as e:
        from walkthru.evidence.capture import missing_extra

        raise missing_extra("pillow", "evidence", purpose="Drawing a highlight") from e
    return Image, ImageChops, ImageDraw, ImageStat


def apply_marker(
    image: Any,
    rects: Sequence[Sequence[float]],
    progress: float = 1.0,
    *,
    color: tuple[int, int, int] = HIGHLIGHTER_YELLOW,
    dark_color: tuple[int, int, int] = DARK_PAGE_AMBER,
    dark_threshold: float = 110.0,
    dark_tint: float = 0.4,
    pad: tuple[float, float] = (0.12, 0.08),
    underline: float = 0.12,
) -> Any:
    """Return a copy of ``image`` (a PIL image) with the marker drawn over ``rects``.

    Args:
        image: The page image (any mode; the result is RGB).
        rects: Boxes ``(x0, y0, x1, y1)`` in image pixels, in reading order.
        progress: How much of the passage is marked, 0..1, by total box width.
        color: Multiply colour on light backgrounds.
        dark_color: Tint and underline colour on dark backgrounds.
        dark_threshold: Mean luminance (0..255) under a box below which it counts as dark.
        dark_tint: Blend weight of ``dark_color`` on dark backgrounds.
        pad: Horizontal and vertical padding around each box, as fractions of its height.
        underline: Underline thickness on dark backgrounds, as a fraction of box height.
    """
    Image, ImageChops, ImageDraw, ImageStat = _pil()
    out = image.convert("RGB")
    progress = min(1.0, max(0.0, float(progress)))
    total = sum(max(0.0, r[2] - r[0]) for r in rects)
    budget = progress * total
    if budget <= 0:
        return out
    W, H = out.size
    for x0, y0, x1, y1 in rects:
        if budget <= 0:
            break
        width = max(0.0, x1 - x0)
        xe = x0 + min(width, budget)
        budget -= width
        h = y1 - y0
        px, py = pad[0] * h, pad[1] * h
        top, bottom = max(0, int(y0 - py)), min(H, int(round(y1 + py)))
        left, right = max(0, int(x0 - px)), min(W, int(round(xe + px)))
        if right <= left or bottom <= top:
            continue
        under = out.crop(
            (max(0, int(x0)), top, min(W, max(int(x0) + 1, int(x1))), bottom)
        )
        luminance = ImageStat.Stat(under.convert("L")).mean[0]
        region = out.crop((left, top, right, bottom))
        if luminance < dark_threshold:
            region = Image.blend(
                region, Image.new("RGB", region.size, dark_color), dark_tint
            )
            thick = max(2, int(round(underline * h)))
            ImageDraw.Draw(region).rectangle(
                (0, region.size[1] - thick, region.size[0], region.size[1]),
                fill=dark_color,
            )
        else:
            region = ImageChops.multiply(region, Image.new("RGB", region.size, color))
        out.paste(region, (left, top))
    return out

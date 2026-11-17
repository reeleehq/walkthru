"""Capture a PDF page at a quoted passage with PyMuPDF, re-boxing scanned pages by OCR.

**Born-digital PDFs.** The page's words (``page.get_text("words")``) are matched against the quote
on their :func:`~walkthru.evidence.text.skeleton` s, so a line-break hyphen never prevents a
match and every letter must agree. The passage's boxes are the boxes of exactly those words,
merged into one bar per line (``verified_by="pdf-text"``). The page is rendered and cropped to a
16:9 window around the passage.

**Scanned PDFs.** When an image covers the page (``scan_coverage``, default 90 %), the text layer
came from someone else's OCR and its boxes can sit tens of pixels off the glyphs: a highlight that
passes a text check lands beside the words. Those boxes are therefore never used. The rendered
window is read again with the ``ocr`` seam (word boxes from pixels), the quote is fuzzy-matched
against the OCR words, and the result is accepted only at ``ocr_min_ratio`` or better
(``verified_by="ocr"``); otherwise the capture is ``found=False``. The default ``ocr`` uses
``ocracy`` with Tesseract — install ``walkthru[ocr]``.
"""

from __future__ import annotations

import urllib.request
import warnings
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union
from urllib.parse import urlparse

from walkthru.evidence.capture import Capture, missing_extra
from walkthru.evidence.text import (
    Box,
    find_word_run,
    fuzzy_word_run,
    merge_line_boxes,
    normalize_text,
)

__all__ = [
    "OcrWords",
    "capture_pdf",
    "ocracy_words",
    "page_image_coverage",
]

#: An OCR function: image path -> ``[(word, (x0, y0, x1, y1)), ...]`` in that image's pixels.
OcrWords = Callable[[Path], Sequence[tuple[str, Box]]]


def ocracy_words(png: Path, *, backend: str = "tesseract") -> list[tuple[str, Box]]:
    """Word boxes read from ``png`` by ``ocracy`` (the default ``ocr`` seam)."""
    try:
        import ocracy
    except ImportError as e:
        raise missing_extra(
            "ocracy", "ocr", purpose="Verifying a scanned PDF page"
        ) from e
    result = ocracy.ocr(str(png), backend=backend)
    return [
        (w.text, w.bbox.as_tuple) for w in result.words if w.bbox is not None and w.text
    ]


def _fitz():
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # PyMuPDF before 1.24 is importable only as ``fitz``
        except ImportError as e:
            raise missing_extra("pymupdf", "evidence", purpose="Capturing a PDF") from e
    return pymupdf


def page_image_coverage(page: Any) -> float:
    """Largest fraction of ``page`` (a PyMuPDF page) covered by a single image, 0..1.

    A scan is one image over the whole page; a born-digital page with a figure is not.
    """
    area = page.rect.width * page.rect.height or 1.0
    best = 0.0
    for info in page.get_image_info():
        r = _fitz().Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            best = max(best, (r.width * r.height) / area)
    return best


def _local_pdf(source: Union[str, Path], out_png: Path) -> Path:
    """A local path for ``source``, downloading an ``http(s)`` PDF next to the output first."""
    s = str(source)
    parsed = urlparse(s)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path))
    if parsed.scheme in ("http", "https"):
        dest = out_png.with_suffix(".source.pdf")
        with urllib.request.urlopen(s) as r:  # noqa: S310 - the caller chose the URL
            dest.write_bytes(r.read())
        return dest
    return Path(s)


def _window(
    page_rect: Any, hits: Sequence[Box], *, aspect: float, min_height_frac: float
) -> tuple[float, float, float, float]:
    """A window ``(x0, y0, w, h)`` in PDF points, ``aspect`` wide, around ``hits``.

    Its height is at least ``min_height_frac`` of the page (so the reader sees context, not a
    sentence floating on white), and the window may be wider than the page: the sides are padded.
    """
    pw, ph = page_rect.width, page_rect.height
    h = (
        min(ph, max(pw / aspect, ph * min_height_frac))
        if hits
        else min(ph, pw / aspect)
    )
    w = h * aspect
    cy = (min(r[1] for r in hits) + max(r[3] for r in hits)) / 2 if hits else h / 2
    y0 = max(0.0, min(ph - h, cy - h / 2))
    return (pw - w) / 2, y0, w, h


def _render_window(page: Any, win, *, zoom: float, png: Path) -> tuple[int, int]:
    """Render ``win`` of ``page`` at ``zoom`` onto a white canvas; returns the image size."""
    fitz = _fitz()
    x0, y0, w, h = win
    clip = fitz.Rect(max(0.0, x0), y0, min(page.rect.width, x0 + w), y0 + h)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    W, H = int(round(w * zoom)), int(round(h * zoom))
    canvas = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, W, H), False)
    canvas.clear_with(255)
    pix.set_origin(int(round((clip.x0 - x0) * zoom)), 0)
    canvas.copy(pix, pix.irect)
    canvas.save(str(png))
    return W, H


def _ocr_rebox(
    png: Path, quote: str, *, ocr: OcrWords, min_ratio: float
) -> tuple[Optional[list[Box]], dict]:
    words = list(ocr(png))
    run = fuzzy_word_run([w for w, _ in words], quote)
    if run is None:
        return None, {"ocr_score": 0.0}
    i, j, score = run
    details = {
        "ocr_score": round(score, 1),
        "ocr_text": " ".join(w for w, _ in words[i : j + 1]),
    }
    if score < min_ratio:
        return None, details
    return merge_line_boxes(b for _, b in words[i : j + 1]), details


def capture_pdf(
    source: Union[str, Path],
    quote: Optional[str] = None,
    *,
    out: Union[str, Path],
    nth: Optional[int] = None,
    page: Optional[int] = None,
    zoom: float = 3.0,
    aspect: float = 16 / 9,
    min_height_frac: float = 0.55,
    ocr: Optional[OcrWords] = ocracy_words,
    scan_coverage: float = 0.9,
    ocr_min_ratio: float = 85.0,
) -> Capture:
    """Render the PDF page holding ``quote`` as a 16:9 window around it, with verified boxes.

    Args:
        source: Local path, ``file://`` URL or ``http(s)://`` URL of a PDF.
        quote: The passage to locate; ``None`` renders the top of ``page`` (or page 0).
        out: Image path (written as PNG) — a ``.json`` sidecar is written beside it.
        nth: Which occurrence to use, counted through the document (or ``page``); default 0.
        page: Search only this page (0-based).
        zoom: Render scale (3.0 ≈ 216 dpi).
        aspect: Width / height of the rendered window.
        min_height_frac: The window is at least this fraction of the page's height.
        ocr: Word-box OCR for scanned pages (default :func:`ocracy_words`); ``None`` refuses to
            verify scans.
        scan_coverage: A page whose largest image covers at least this fraction is a scan.
        ocr_min_ratio: Minimum fuzzy score (0..100) for OCR words to count as the quote.
    """
    fitz = _fitz()
    png = Path(out).with_suffix(".png")
    png.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(_local_pdf(source, png)))
    pages = [page] if page is not None else range(len(doc))

    hit_page, words, run, details = (page or 0), [], None, {}
    if quote and normalize_text(quote):
        remaining = nth or 0
        for pno in pages:
            pw = doc[pno].get_text("words")
            k = 0
            while (r := find_word_run([w[4] for w in pw], quote, nth=k)) is not None:
                if remaining == 0:
                    hit_page, words, run = pno, pw, r
                    break
                remaining -= 1
                k += 1
            if run is not None:
                break

    pg = doc[hit_page]
    word_boxes = [tuple(w[:4]) for w in words[run[0] : run[1] + 1]] if run else []
    win = _window(pg.rect, word_boxes, aspect=aspect, min_height_frac=min_height_frac)
    size = _render_window(pg, win, zoom=zoom, png=png)

    def to_image(b: Sequence[float]) -> Box:
        x0, y0 = win[0], win[1]
        return (
            (b[0] - x0) * zoom,
            (b[1] - y0) * zoom,
            (b[2] - x0) * zoom,
            (b[3] - y0) * zoom,
        )

    rects: tuple[Box, ...] = ()
    verified_by, reason = None, "no-quote"
    matched = " ".join(w[4] for w in words[run[0] : run[1] + 1]) if run else None
    if quote and normalize_text(quote):
        reason = "not-in-text"
    if run is not None:
        coverage = page_image_coverage(pg)
        details["image_coverage"] = round(coverage, 3)
        if coverage < scan_coverage:
            lines: dict[tuple[int, int], list] = {}
            for w in words[run[0] : run[1] + 1]:
                lines.setdefault((w[5], w[6]), []).append(w[:4])
            line_boxes = [
                (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
                for bs in lines.values()
            ]  # fmt: skip
            rects = tuple(to_image(b) for b in line_boxes)
            verified_by, reason = "pdf-text", None
        elif ocr is None:
            reason = "scanned-needs-ocr"
        else:
            try:
                boxes, ocr_details = _ocr_rebox(
                    png, quote, ocr=ocr, min_ratio=ocr_min_ratio
                )
            except ImportError as e:
                warnings.warn(f"{e} — showing the scanned page without a highlight.")
                boxes, ocr_details, reason = None, {}, "scanned-needs-ocr"
            else:
                reason = "ocr-mismatch"
            details.update(ocr_details)
            if boxes:
                rects, verified_by, reason = tuple(boxes), "ocr", None
                matched = ocr_details.get("ocr_text", matched)

    capture = Capture(
        png=png,
        size=size,
        rects=rects,
        found=bool(rects),
        matched=matched,
        verified_by=verified_by,
        source=str(source),
        quote=quote,
        reason=reason,
        page=hit_page,
        nth=(nth or 0) if run is not None else None,
        details=details,
    )
    capture.save()
    return capture

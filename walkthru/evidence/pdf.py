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
from typing import Any, Callable, MutableMapping, Optional, Sequence, Union
from urllib.parse import urlparse

from walkthru.evidence.capture import Capture, missing_extra
from walkthru.evidence.text import (
    Box,
    find_word_run,
    fuzzy_word_run,
    merge_line_boxes,
    normalize_text,
    tokens_agree,
)

__all__ = [
    "OcrWords",
    "capture_pdf",
    "has_invisible_text",
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
    """Fraction of ``page`` (a PyMuPDF page) covered by the union of its images, 0..1.

    A scan is images over the whole page (sometimes in strips); a born-digital page with a
    figure is not.
    """
    fitz = _fitz()
    rects = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            rects.append(r)
    area = page.rect.width * page.rect.height or 1.0
    return min(1.0, _union_area(rects) / area)


def _union_area(rects: Sequence[Any]) -> float:
    """Exact area of a union of rectangles (coordinate compression; images per page are few)."""
    xs = sorted({v for r in rects for v in (r.x0, r.x1)})
    total = 0.0
    for xa, xb in zip(xs, xs[1:]):
        spans = sorted((r.y0, r.y1) for r in rects if r.x0 <= xa and r.x1 >= xb)
        covered, end = 0.0, None
        for y0, y1 in spans:
            if end is None or y0 > end:
                covered += y1 - y0
                end = y1
            elif y1 > end:
                covered += y1 - end
                end = y1
        total += covered * (xb - xa)
    return total


def has_invisible_text(page: Any) -> bool:
    """True when ``page`` holds invisible text (render mode 3): the mark of an OCR layer."""
    try:
        return any(span.get("type") == 3 for span in page.get_texttrace())
    except AttributeError:  # pragma: no cover - PyMuPDF older than 1.19
        return False


def _local_pdf(source: Union[str, Path], out_png: Path, *, timeout: float) -> Path:
    """A local path for ``source``, downloading an ``http(s)`` PDF next to the output first."""
    s = str(source)
    parsed = urlparse(s)
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path))
    if parsed.scheme in ("http", "https"):
        dest = out_png.with_suffix(".source.pdf")
        with urllib.request.urlopen(s, timeout=timeout) as r:  # noqa: S310 - caller's URL
            dest.write_bytes(r.read())
        return dest
    return Path(s)


def _window(
    page_rect: Any, hits: Sequence[Box], *, aspect: float, min_height_frac: float
) -> tuple[float, float, float, float]:
    """A window ``(x0, y0, w, h)`` in (rotated) page points, ``aspect`` wide, holding ``hits``.

    Its height is at least ``min_height_frac`` of the page (the reader sees context, not a
    sentence floating on white) and at least the passage's height with a margin; it is wide
    enough for the passage. It is centred on the passage where the page allows, and may extend
    past the page, which is then padded.
    """
    pw, ph = page_rect.width, page_rect.height
    if not hits:
        h = min(ph, pw / aspect)
        return (pw - h * aspect) / 2, 0.0, h * aspect, h
    hx0, hy0 = min(r[0] for r in hits), min(r[1] for r in hits)
    hx1, hy1 = max(r[2] for r in hits), max(r[3] for r in hits)
    h = max(min(ph, max(pw / aspect, ph * min_height_frac)), (hy1 - hy0) * 1.25)
    w = max(h * aspect, (hx1 - hx0) * 1.05)
    h = w / aspect

    def place(size: float, extent: float, centre: float) -> float:
        if size >= extent:
            return (extent - size) / 2
        return max(0.0, min(extent - size, centre - size / 2))

    return place(w, pw, (hx0 + hx1) / 2), place(h, ph, (hy0 + hy1) / 2), w, h


def _render_window(page: Any, win, *, zoom: float, png: Path) -> tuple[int, int]:
    """Render ``win`` of ``page`` at ``zoom`` onto a white canvas; returns the image size."""
    fitz = _fitz()
    x0, y0, w, h = win
    clip = fitz.Rect(x0, y0, x0 + w, y0 + h) & page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    W, H = int(round(w * zoom)), int(round(h * zoom))
    canvas = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, W, H), False)
    canvas.clear_with(255)
    pix.set_origin(int(round((clip.x0 - x0) * zoom)), int(round((clip.y0 - y0) * zoom)))
    canvas.copy(pix, pix.irect)
    canvas.save(str(png))
    return W, H


def _ocr_rebox(
    png: Path,
    quote: str,
    near: Sequence[Box],
    *,
    ocr: OcrWords,
    min_ratio: float,
) -> tuple[Optional[list[Box]], dict]:
    """Boxes of ``quote`` read by OCR from ``png``, near the text layer's (image-pixel) ``near``.

    Only OCR words within a few line heights of where the text layer put the passage are
    considered, so the occurrence chosen by the text layer (``nth``) is the one boxed. Accepted
    when the whole-string score reaches ``min_ratio`` *and* the words agree one by one.
    """
    line_h = max(r[3] - r[1] for r in near)
    top = min(r[1] for r in near) - 3 * line_h
    bottom = max(r[3] for r in near) + 3 * line_h
    words = [(w, b) for w, b in ocr(png) if top <= (b[1] + b[3]) / 2 <= bottom]
    run = fuzzy_word_run([w for w, _ in words], quote)
    if run is None:
        return None, {"ocr_score": 0.0}
    i, j, score = run
    text = " ".join(w for w, _ in words[i : j + 1])
    details = {"ocr_score": round(score, 1), "ocr_text": text}
    if score < min_ratio or not tokens_agree(text, quote):
        return None, details
    return merge_line_boxes(b for _, b in words[i : j + 1]), details


def _line_ends(words: Sequence[tuple]) -> list[bool]:
    """For each PyMuPDF word tuple, whether it is the last word of its (block, line)."""
    keys = [(w[5], w[6]) for w in words]
    return [i == len(keys) - 1 or keys[i] != keys[i + 1] for i in range(len(keys))]


def _ocr_search(
    doc: Any,
    quote: str,
    pages: Sequence[int],
    *,
    ocr: OcrWords,
    zoom: float,
    scan_coverage: float,
    min_ratio: float,
    max_pages: int,
    cache: Optional[MutableMapping],
) -> tuple[Optional[tuple[int, list[Box]]], dict]:
    """Find ``quote`` by OCR on scanned ``pages`` whose text layer does not hold it.

    Only scans (image coverage >= ``scan_coverage``, or invisible text) are read, at most
    ``max_pages`` of them, in order. A page's OCR words are cached by the hash of its rendered
    image in ``cache`` (any mapping) when one is given. Returns ``((page, boxes_in_page_points),
    details)``, the first element ``None`` when no page matched; ``details`` records what was tried.
    """
    import hashlib
    import tempfile

    fitz = _fitz()
    details: dict[str, Any] = {"ocr_pages_tried": []}
    with tempfile.TemporaryDirectory(prefix="walkthru_ocr_") as tmp:
        for pno in pages:
            pg = doc[pno]
            if not (page_image_coverage(pg) >= scan_coverage or has_invisible_text(pg)):
                continue
            if len(details["ocr_pages_tried"]) >= max_pages:
                details["ocr_page_limit"] = max_pages
                break
            details["ocr_pages_tried"].append(pno)
            png = Path(tmp) / f"page{pno}.png"
            pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).save(str(png))
            key = hashlib.sha1(png.read_bytes()).hexdigest()
            if cache is not None and key in cache:
                words = cache[key]
            else:
                words = [(w, tuple(b)) for w, b in ocr(png)]
                if cache is not None:
                    cache[key] = words
            run = fuzzy_word_run([w for w, _ in words], quote)
            if run is None:
                continue
            i, j, score = run
            text = " ".join(w for w, _ in words[i : j + 1])
            if score < min_ratio or not tokens_agree(text, quote):
                details["ocr_score"] = max(
                    details.get("ocr_score", 0.0), round(score, 1)
                )
                continue
            boxes = merge_line_boxes(b for _, b in words[i : j + 1])
            details.update(ocr_score=round(score, 1), ocr_text=text)
            return (pno, [tuple(c / zoom for c in b) for b in boxes]), details
    return None, details


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
    ocr_search: bool = False,
    max_ocr_pages: int = 5,
    ocr_cache: Optional[MutableMapping] = None,
    timeout: float = 60.0,
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
        scan_coverage: A page whose images cover at least this fraction is a scan; so is a
            page holding invisible text (an OCR layer).
        ocr_min_ratio: Minimum fuzzy score (0..100) for OCR words to count as the quote; the
            words must also agree one by one (:func:`~walkthru.evidence.text.tokens_agree`).
        ocr_search: When no page's text layer holds the quote, OCR the scanned pages (``page``
            only, when given) and look for it there: for scans whose embedded text is missing or
            too garbled. Opt-in, since it costs one OCR pass per page; ``max_ocr_pages`` bounds
            it, and ``ocr_cache`` (any mapping) keeps each page's words, keyed by the page
            image's hash, so a rerun reads nothing twice.
        max_ocr_pages: At most this many scanned pages are OCR-searched.
        ocr_cache: Optional ``MutableMapping`` caching OCR words per page image.
        timeout: Seconds allowed to download an ``http(s)`` PDF.
    """
    fitz = _fitz()
    png = Path(out).with_suffix(".png")
    png.parent.mkdir(parents=True, exist_ok=True)
    wanted = bool(quote and normalize_text(quote))
    with fitz.open(str(_local_pdf(source, png, timeout=timeout))) as doc:
        hit_page, words, run = (page or 0), [], None
        if wanted:
            remaining = nth or 0
            for pno in [page] if page is not None else range(len(doc)):
                pw = doc[pno].get_text("words")
                texts, ends = [w[4] for w in pw], _line_ends(pw)
                k = 0
                while (
                    r := find_word_run(texts, quote, nth=k, line_ends=ends)
                ) is not None:
                    if remaining == 0:
                        hit_page, words, run = pno, pw, r
                        break
                    remaining, k = remaining - 1, k + 1
                if run is not None:
                    break
        ocr_hit: Optional[tuple[int, list[Box]]] = None
        search_details: dict[str, Any] = {}
        if wanted and run is None and ocr_search and ocr is not None:
            try:
                ocr_hit, search_details = _ocr_search(
                    doc,
                    quote,
                    [page] if page is not None else list(range(len(doc))),
                    ocr=ocr,
                    zoom=zoom,
                    scan_coverage=scan_coverage,
                    min_ratio=ocr_min_ratio,
                    max_pages=max_ocr_pages,
                    cache=ocr_cache,
                )
            except ImportError as e:  # the ocr extra is not installed
                warnings.warn(f"{e}: not searching scanned pages by OCR.")
            except Exception as e:  # e.g. the OCR engine's binary is missing
                warnings.warn(f"OCR search failed ({e}).")
                search_details = {"ocr_error": str(e)}
            if ocr_hit is not None:
                hit_page = ocr_hit[0]
        pg = doc[hit_page]
        hit_words = words[run[0] : run[1] + 1] if run else []
        rot = pg.rotation_matrix  # word boxes are unrotated; the rendered page is not
        lines: dict[tuple[int, int], Any] = {}
        for w in hit_words:
            r = fitz.Rect(w[:4]) * rot
            key = (w[5], w[6])
            lines[key] = lines[key] | r if key in lines else r
        line_boxes = [(r.x0, r.y0, r.x1, r.y1) for r in lines.values()]
        if ocr_hit is not None:
            line_boxes = ocr_hit[1]
        win = _window(
            pg.rect, line_boxes, aspect=aspect, min_height_frac=min_height_frac
        )
        size = _render_window(pg, win, zoom=zoom, png=png)

        def to_image(b: Sequence[float]) -> Box:
            x0, y0 = win[0], win[1]
            return ((b[0] - x0) * zoom, (b[1] - y0) * zoom, (b[2] - x0) * zoom, (b[3] - y0) * zoom)  # fmt: skip

        rects: tuple[Box, ...] = ()
        details: dict[str, Any] = {}
        verified_by, reason = None, ("not-in-text" if wanted else "no-quote")
        matched = " ".join(w[4] for w in hit_words) if run else None
        details.update(search_details)
        if ocr_hit is not None:
            rects = tuple(to_image(b) for b in line_boxes)
            verified_by, reason = "ocr", None
            matched = search_details.get("ocr_text")
            details.update(
                image_coverage=round(page_image_coverage(pg), 3),
                found_by="ocr-search",
            )
        if run is not None:
            coverage = page_image_coverage(pg)
            invisible = has_invisible_text(pg)
            details.update(image_coverage=round(coverage, 3), invisible_text=invisible)
            candidate = tuple(to_image(b) for b in line_boxes)
            scanned = coverage >= scan_coverage or invisible
            if (
                scanned
            ):  # diagnostic only: where the untrusted text layer put the passage
                details["text_layer_rects"] = [list(map(float, r)) for r in candidate]
            if not scanned:
                rects, verified_by, reason = candidate, "pdf-text", None
            elif ocr is None:
                reason = "scanned-needs-ocr"
            else:
                try:
                    boxes, ocr_details = _ocr_rebox(
                        png, quote, candidate, ocr=ocr, min_ratio=ocr_min_ratio
                    )
                    reason = "ocr-mismatch"
                except ImportError as e:  # the ocr extra is not installed
                    warnings.warn(f"{e}: showing the scanned page without a highlight.")
                    boxes, ocr_details, reason = None, {}, "scanned-needs-ocr"
                except Exception as e:  # e.g. the OCR engine's binary is missing
                    warnings.warn(
                        f"OCR failed ({e}): showing the page without a highlight."
                    )
                    boxes, ocr_details, reason = (
                        None,
                        {"ocr_error": str(e)},
                        "ocr-failed",
                    )
                details.update(ocr_details)
                if boxes:
                    rects, verified_by, reason = tuple(boxes), "ocr", None
                    matched = ocr_details.get("ocr_text", matched)
        W, H = size
        if rects and not all(
            r[0] >= -1 and r[1] >= -1 and r[2] <= W + 1 and r[3] <= H + 1 for r in rects
        ):
            rects, verified_by, reason = (), None, "off-screen"

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

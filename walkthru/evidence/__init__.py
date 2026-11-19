"""Show a cited source on screen with the quoted passage highlighted, in sync with narration.

Two calls::

    from walkthru import capture_passage, render_highlight_clip

    cap = capture_passage("https://example.org/article", quote="the exact words", out="shots/a.png")
    render_highlight_clip(cap, "clips/a.mp4", duration=8.0, sweep=(1.0, 3.6))

:func:`capture_passage` screenshots a web page (Playwright) or renders a PDF page (PyMuPDF) at the
quote and returns a :class:`Capture` whose boxes are **verified** to hold the quote; scanned PDF
pages are re-boxed from OCR (``ocracy``). :func:`render_highlight_clip` eases the camera toward
the passage while a highlighter sweeps across it between ``sweep[0]`` and ``sweep[1]`` seconds.

The rule this module is built around: **highlight only what verification passed.** A capture with
``found=False`` has no boxes; show that page without a highlight, and say so.

Like the adapters, this module imports its vendors (Playwright, PyMuPDF, Pillow, ocracy) only
when a function needs them, so importing ``walkthru`` stays dependency-free. Install with
``pip install "walkthru[evidence]"`` (plus ``walkthru[ocr]`` for scanned PDFs).
"""

from walkthru.evidence.capture import (
    Capture,
    EvidenceDependencyError,
    capture_passage,
    is_pdf_source,
)
from walkthru.evidence.clip import (
    iter_highlight_frames,
    render_highlight_clip,
    smoothstep,
    target_window,
)
from walkthru.evidence.html import (
    DEFAULT_DISMISS_LABELS,
    PASSAGE_LOCATE_JS,
    PASSAGE_VERIFY_JS,
    capture_html,
    judge_passage,
    locate_args,
    open_capture_page,
)
from walkthru.evidence.marker import apply_marker
from walkthru.evidence.pdf import (
    capture_pdf,
    has_invisible_text,
    ocracy_words,
    page_image_coverage,
)
from walkthru.evidence.text import (
    find_word_run,
    fuzzy_word_run,
    merge_line_boxes,
    normalize_text,
    skeleton,
    tokens_agree,
)

__all__ = [
    "Capture",
    "EvidenceDependencyError",
    "capture_passage",
    "capture_html",
    "capture_pdf",
    "open_capture_page",
    "DEFAULT_DISMISS_LABELS",
    "PASSAGE_LOCATE_JS",
    "PASSAGE_VERIFY_JS",
    "locate_args",
    "judge_passage",
    "has_invisible_text",
    "tokens_agree",
    "render_highlight_clip",
    "iter_highlight_frames",
    "apply_marker",
    "ocracy_words",
    "page_image_coverage",
    "is_pdf_source",
    "normalize_text",
    "skeleton",
    "merge_line_boxes",
    "find_word_run",
    "fuzzy_word_run",
    "smoothstep",
    "target_window",
]
